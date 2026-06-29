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


SAMPLE_ACTION_OUTPUT = """Let me explore a few specific pages that look promising, including the Snowplow blog from Databricks Summit, and search for newer specific articles.Action:
{"name": "webpage_content", "arguments": {"url": "https://snowplow.io/blog/takeaways-from-databricks-data-ai-summit-2026", "query": "vector database RAG AI", "query_type": "relevant"}}
{"name": "web_search", "arguments": {"query": "\\"AI agents\\" vector database embedding data engineering substack June 2026"}}
{"name": "web_search", "arguments": {"query": "RAG architecture retrieval augmented generation guide blog post June 26 27 28 2026"}}
{"name": "web_search", "arguments": {"query": "Weaviate Milvus blog launch release June 2026"}}"""


SAMPLE_CALLING_TOOLS_JSON_LINES = """Calling tools:
[{"name": "web_search", "arguments": {"query": "AI agents vector database embedding data engineering substack June 2026"}}, {"name": "web_search", "arguments": {"query": "RAG architecture retrieval augmented generation blog June 26 27 28 2026"}}, {"name": "web_search", "arguments": {"query": "Weaviate Milvus blog launch release June 2026"}}, {"name": "webpage_content", "arguments": {"url": "https://snowplow.io/blog/takeaways-from-databricks-data-ai-summit-2026", "query": "AI infrastructure vector database RAG", "query_type": "relevant"}}]"""


SAMPLE_MALFORMED_CALLING_TOOLS = """Calling tools:
[{'id': 'call_hB91IKcLOcPjxAYkhr3wgrUv', 'type': 'function', 'function': {'name': 'web_search', 'arguments': {'query': 'AI agents vector database embedding data engineering substack June 2026'}}}, {'id': 'call_zqEsRUIzMK8LZYe07gAdEVAQ', 'type': 'function', 'function': {'name': 'web_search', 'arguments': {'query': 'RAG architecture retrieval augmented generation blog June 26 27 28 2026'}}, {'id': 'call_tJDd3RbTzFTyYcKWhvRWpfqH', 'type': 'function', 'function': {'name': 'web_search', 'arguments': {'query': 'Weaviate Milvus blog launch release June 2026'}}, {'id': 'call_WOmHo7kUDaULqQ2Z5XS1lbYK', 'type': 'function', 'function': {'name': 'webpage_content', 'arguments': {'url': 'https://snowplow.io/blog/takeaways-from-databricks-data-ai-summit-2026', 'query': 'AI infrastructure vector database RAG', 'query_type': 'relevant'}}}]"""


def test_recovers_multiple_tool_calls_from_action_block():
    cleaned, tool_calls = recover_tool_calls_from_text(SAMPLE_ACTION_OUTPUT)

    assert cleaned.startswith("Let me explore a few specific pages")
    assert "Action:" not in (cleaned or "")
    assert len(tool_calls) == 4
    assert tool_calls[0].function.name == "webpage_content"
    assert "snowplow.io" in tool_calls[0].function.arguments["url"]
    assert tool_calls[1].function.name == "web_search"
    assert "Weaviate Milvus" in tool_calls[3].function.arguments["query"]


def test_recovers_tool_calls_from_calling_tools_json_array():
    cleaned, tool_calls = recover_tool_calls_from_text(SAMPLE_CALLING_TOOLS_JSON_LINES)

    assert cleaned is None or cleaned == ""
    assert len(tool_calls) == 4
    assert tool_calls[-1].function.name == "webpage_content"


def test_recovers_tool_calls_from_malformed_calling_tools_blob():
    cleaned, tool_calls = recover_tool_calls_from_text(SAMPLE_MALFORMED_CALLING_TOOLS)

    assert len(tool_calls) >= 2
    assert tool_calls[0].function.name == "web_search"
    assert "substack June 2026" in tool_calls[0].function.arguments["query"]


def test_parse_tool_calls_recovers_action_format():
    model = RetryOpenAIServerModel.__new__(RetryOpenAIServerModel)
    model.model = RetryOpenAIServerModel.__new__(RetryOpenAIServerModel)
    response = ChatMessage(role="assistant", content=SAMPLE_ACTION_OUTPUT)

    parsed = model.parse_tool_calls(response)

    assert parsed.tool_calls is not None
    assert len(parsed.tool_calls) == 4


SAMPLE_PROSE_JSON_LINES = """I found the Rio Rundown pages. Now let me get content from specific articles with their full details.
{"name": "webpage_content", "arguments": {"url": "https://riorundown.substack.com/p/trending-ai-news-jun-27-2026", "query": "Trending AI News June 27 2026 description data engineering infrastructure", "query_type": "fullcontent"}}
{"name": "webpage_content", "arguments": {"url": "https://riorundown.substack.com/p/trending-ai-news-jun-26-2026", "query": "Trending AI News June 26 2026 description data engineering infrastructure", "query_type": "fullcontent"}}
{"name": "web_search", "arguments": {"query": "riorundown substack trending ai news june 23 2026"}}
{"name": "web_search", "arguments": {"query": "headlinesbriefing substack friday june 26 2026 data engineering"}}
{"name": "http_get", "arguments": {"url": "https://smartchunks.com/azure-databricks-renames-vector-search-ai-search-june-2026/", "timeout": 60}}"""


def test_recovers_tool_calls_from_prose_without_action_marker():
    cleaned, tool_calls = recover_tool_calls_from_text(SAMPLE_PROSE_JSON_LINES)

    assert cleaned.startswith("I found the Rio Rundown pages")
    assert len(tool_calls) == 5
    assert tool_calls[0].function.name == "webpage_content"
    assert tool_calls[-1].function.name == "http_get"
