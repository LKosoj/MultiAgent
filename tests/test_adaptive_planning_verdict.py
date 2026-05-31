"""Tests for parse_verdict and evaluate_plan_adherence."""
import pytest
from unittest.mock import MagicMock

from smolagents.monitoring import TokenUsage

from adaptive_planning import Verdict, parse_verdict, evaluate_plan_adherence


# ---------------------------------------------------------------------------
# parse_verdict — keyword fallback устойчив к негативным формулировкам
# (срабатывает только без распознанного JSON; негатив => fail-safe replan)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text",
    [
        "The agent is NOT on track",
        "The agent is not on track true",          # 'true' не должно перебить негатив
        "not going on track right now",            # разрыв 'not ... on track'
        "the run went off track",                  # 'off track' маркер дрейфа
        "Monitor decision: replan",
        "on_track is false here",
        "needs to replan immediately",
    ],
)
def test_keyword_fallback_negative_phrases_force_replan(text):
    v = parse_verdict(text)
    assert v.replan_needed is True
    assert v.on_track is False


def test_keyword_fallback_positive_on_track():
    v = parse_verdict("The agent is clearly on track and proceeding")
    assert v.on_track is True
    assert v.replan_needed is False


# ---------------------------------------------------------------------------
# parse_verdict — вложенный JSON-объект разбирается целиком (raw_decode)
# ---------------------------------------------------------------------------

def test_nested_json_object_parsed_whole():
    v = parse_verdict('{"on_track": true, "data": {"x": 1}, "reason": "fine"}')
    assert v.on_track is True
    assert v.replan_needed is False


def test_foreign_object_before_verdict_skipped():
    v = parse_verdict('noise {"foo": 1} then {"on_track": false, "reason": "drift"}')
    assert v.on_track is False
    assert v.replan_needed is True


# ---------------------------------------------------------------------------
# parse_verdict — JSON paths
# ---------------------------------------------------------------------------

def test_json_on_track_true():
    v = parse_verdict('{"on_track": true, "reason": "looks good"}')
    assert v.on_track is True
    assert v.replan_needed is False
    assert "looks good" in v.reason


def test_json_on_track_false():
    v = parse_verdict('{"on_track": false, "reason": "lost"}')
    assert v.on_track is False
    assert v.replan_needed is True


def test_json_replan_needed_explicit():
    v = parse_verdict('{"on_track": false, "replan_needed": true, "reason": "err"}')
    assert v.replan_needed is True


def test_json_on_track_true_replan_false_explicit():
    v = parse_verdict('{"on_track": true, "replan_needed": false, "reason": "ok"}')
    assert v.on_track is True
    assert v.replan_needed is False


def test_json_embedded_in_text():
    text = 'Thinking... {"on_track": true, "reason": "fine"} done.'
    v = parse_verdict(text)
    assert v.on_track is True
    assert v.replan_needed is False


# ---------------------------------------------------------------------------
# parse_verdict — foreign object before the real verdict object
# ---------------------------------------------------------------------------

def test_foreign_json_object_skipped():
    """First JSON has no on_track/replan_needed — should be skipped."""
    text = '{"foo": "bar"} then {"on_track": true, "reason": "ok"}'
    v = parse_verdict(text)
    assert v.on_track is True
    assert v.replan_needed is False


def test_foreign_json_object_skipped_replan():
    text = '{"meta": 1} {"on_track": false, "reason": "err"}'
    v = parse_verdict(text)
    assert v.on_track is False
    assert v.replan_needed is True


# ---------------------------------------------------------------------------
# parse_verdict — keyword fallback
# ---------------------------------------------------------------------------

def test_keyword_not_on_track_returns_replan():
    v = parse_verdict("The agent is not on track.")
    assert v.replan_needed is True
    assert v.on_track is False


def test_keyword_replan_returns_replan():
    v = parse_verdict("We need to replan the approach.")
    assert v.replan_needed is True


def test_keyword_on_track_string_returns_on_track():
    v = parse_verdict("Agent is on track with the plan.")
    assert v.on_track is True
    assert v.replan_needed is False


def test_keyword_on_track_true_in_text():
    v = parse_verdict("on_track true — everything looks fine")
    assert v.on_track is True
    assert v.replan_needed is False


def test_keyword_on_track_false_in_text():
    v = parse_verdict("on_track false something went wrong")
    assert v.replan_needed is True


# ---------------------------------------------------------------------------
# parse_verdict — fail-safe cases
# ---------------------------------------------------------------------------

def test_empty_string_returns_failsafe_replan():
    v = parse_verdict("")
    assert v.replan_needed is True
    assert v.on_track is False


def test_whitespace_only_returns_failsafe_replan():
    v = parse_verdict("   ")
    assert v.replan_needed is True
    assert v.on_track is False


def test_garbage_returns_failsafe_replan():
    v = parse_verdict("xyzzy no useful info here 12345")
    assert v.replan_needed is True
    assert v.on_track is False


# ---------------------------------------------------------------------------
# parse_verdict — return type
# ---------------------------------------------------------------------------

def test_parse_verdict_returns_verdict_instance():
    v = parse_verdict('{"on_track": true, "reason": "ok"}')
    assert isinstance(v, Verdict)


# ---------------------------------------------------------------------------
# evaluate_plan_adherence
# ---------------------------------------------------------------------------

def _make_model_response(content, token_usage=None):
    resp = MagicMock()
    resp.content = content
    resp.token_usage = token_usage
    return resp


def test_evaluate_str_content_on_track():
    model = MagicMock()
    model.generate.return_value = _make_model_response('{"on_track": true, "reason": "fine"}')
    v = evaluate_plan_adherence(model, "task", "plan", "context")
    assert v.on_track is True
    assert v.replan_needed is False


def test_evaluate_list_content_on_track():
    """content as list[{"text": ...}]."""
    model = MagicMock()
    raw = [{"text": '{"on_track": true, "reason": "ok"}'}]
    model.generate.return_value = _make_model_response(raw)
    v = evaluate_plan_adherence(model, "task", "plan", "context")
    assert v.on_track is True
    assert v.replan_needed is False


def test_evaluate_list_content_replan():
    model = MagicMock()
    raw = [{"text": '{"on_track": false, "reason": "err"}'}]
    model.generate.return_value = _make_model_response(raw)
    v = evaluate_plan_adherence(model, "task", "plan", "context")
    assert v.replan_needed is True


def test_evaluate_exception_returns_replan():
    model = MagicMock()
    model.generate.side_effect = RuntimeError("network error")
    v = evaluate_plan_adherence(model, "task", "plan", "context")
    assert v.replan_needed is True
    assert v.on_track is False


def test_evaluate_content_none_returns_replan():
    """resp.content = None → empty text → fail-safe replan."""
    model = MagicMock()
    model.generate.return_value = _make_model_response(None)
    v = evaluate_plan_adherence(model, "task", "plan", "context")
    assert v.replan_needed is True


def test_evaluate_token_usage_propagated():
    model = MagicMock()
    usage = TokenUsage(input_tokens=10, output_tokens=5)
    resp = _make_model_response('{"on_track": true, "reason": "ok"}', token_usage=usage)
    model.generate.return_value = resp
    v = evaluate_plan_adherence(model, "task", "plan", "context")
    assert v.token_usage is usage


def test_evaluate_token_usage_none_when_missing():
    model = MagicMock()
    resp = MagicMock(spec=[])  # no token_usage attribute
    resp.content = '{"on_track": true, "reason": "ok"}'
    model.generate.return_value = resp
    v = evaluate_plan_adherence(model, "task", "plan", "context")
    assert v.token_usage is None


def test_evaluate_list_content_multiple_text_parts_joined():
    """Multiple text chunks in list content are joined with space."""
    model = MagicMock()
    raw = [{"text": '{"on_track":'}, {"text": ' true, "reason": "joined"}'}]
    model.generate.return_value = _make_model_response(raw)
    v = evaluate_plan_adherence(model, "task", "plan", "context")
    assert v.on_track is True
