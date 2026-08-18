"""Тесты для _build_shot_continuity_window: 3-shot окно motion continuity."""

import json
import sys
import types


agent_command_stub = types.ModuleType("agent_command")
agent_command_stub.model_hard = "test-model-hard"
agent_command_stub.model_code = "test-model-code"
agent_command_stub.model_ultimate = "test-model-ultimate"
agent_command_stub.model_lite = "test-model-lite"
sys.modules.setdefault("agent_command", agent_command_stub)

utils_stub = types.ModuleType("utils")
utils_stub.call_openai_api = lambda *args, **kwargs: '{"is_black_screen": false}'
utils_stub.extract_json_from_markdown = lambda text: text
utils_stub.parse_llm_json = lambda text: json.loads(text)
utils_stub.translate_prompts_in_items = lambda *args, **kwargs: args[0]
sys.modules.setdefault("utils", utils_stub)

from custom_tools.storybook.screenplay_shots_generator_utils.shared_utils import (  # noqa: E402
    _build_shot_continuity_window,
)


def test_prev_none_next_present() -> None:
    curr = {
        "shot_number": 1,
        "shot_type": "start",
        "camera_plan": "wide",
        "description": "Герой стоит у двери. Он делает вдох.",
    }
    nxt = {
        "shot_number": 2,
        "shot_type": "start",
        "camera_plan": "medium",
        "description": "Герой открывает дверь. Свет заливает комнату.",
    }

    window = _build_shot_continuity_window(curr, None, nxt)

    assert window["window_size"] == 3
    assert window["prev"] is None
    assert window["curr"]["shot_number"] == 1
    assert window["curr"]["shot_description"] == curr["description"]
    assert window["next"] is not None
    assert window["next"]["shot_number"] == 2
    assert window["next"]["opening_action"] == "Герой открывает дверь"
    assert window["next"]["shot_description_head"] == nxt["description"]


def test_prev_and_next_present_with_truncation() -> None:
    long_prev_desc = "Начало сцены. " + ("A" * 400) + ". Герой падает на колени."
    long_next_desc = "Герой встаёт с пола. " + ("B" * 400) + ". Финальный кадр."
    prev = {
        "shot_number": 4,
        "shot_type": "end",
        "camera_plan": "close",
        "description": long_prev_desc,
    }
    curr = {
        "shot_number": 5,
        "shot_type": "start",
        "camera_plan": "medium",
        "description": "Герой смотрит на дверь.",
    }
    nxt = {
        "shot_number": 6,
        "shot_type": "start",
        "camera_plan": "wide",
        "description": long_next_desc,
    }

    window = _build_shot_continuity_window(curr, prev, nxt)

    assert window["prev"] is not None
    assert window["next"] is not None
    assert len(window["prev"]["shot_description_tail"]) == 240
    assert window["prev"]["shot_description_tail"] == long_prev_desc[-240:]
    assert window["prev"]["final_action"] == "Герой падает на колени"
    assert len(window["next"]["shot_description_head"]) == 240
    assert window["next"]["shot_description_head"] == long_next_desc[:240]
    assert window["next"]["opening_action"] == "Герой встаёт с пола"
    assert window["curr"]["shot_number"] == 5
    assert window["curr"]["camera_plan"] == "medium"


def test_short_description_tail_and_head_equal_full_text() -> None:
    short = "Герой кивает"
    prev = {"shot_number": 1, "camera_plan": "wide", "description": short}
    curr = {"shot_number": 2, "camera_plan": "medium", "description": "..."}
    nxt = {"shot_number": 3, "camera_plan": "close", "description": short}

    window = _build_shot_continuity_window(curr, prev, nxt)

    assert window["prev"]["shot_description_tail"] == short
    assert window["next"]["shot_description_head"] == short
    assert window["prev"]["shot_description_tail"] == window["next"]["shot_description_head"]
    assert window["prev"]["final_action"] == short
    assert window["next"]["opening_action"] == short
