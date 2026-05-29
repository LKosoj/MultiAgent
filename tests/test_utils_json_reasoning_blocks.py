import json
import os

os.environ.setdefault("OPENAI_API_BASE_DB", "http://example.test/v1")
os.environ.setdefault("OPENAI_API_KEY_DB", "test-key")
from utils import extract_json_from_markdown, parse_llm_json


def test_extract_json_from_markdown_strips_closed_think_block() -> None:
    raw = """
    <think>
    Сначала рассуждение с фигурными скобками {"not": "payload"}.
    </think>
    {"status":"ok","items":[1,2,3]}
    """

    assert extract_json_from_markdown(raw) == '{"status":"ok","items":[1,2,3]}'


def test_extract_json_from_markdown_strips_unclosed_think_prefix_before_fence() -> None:
    raw = """
    <think>
    reasoning leakage
    ```json
    {"status":"ok"}
    ```
    """

    assert extract_json_from_markdown(raw) == '{"status":"ok"}'


def test_parse_llm_json_recovers_payload_after_think_leakage() -> None:
    raw = """
    <think>
    reasoning leakage with braces {"foo":"bar"}
    </think>
    {"video_prompt":"single line prompt"}
    """

    parsed = parse_llm_json(raw)

    assert parsed == {"video_prompt": "single line prompt"}
    assert json.dumps(parsed, ensure_ascii=False) == '{"video_prompt": "single line prompt"}'
