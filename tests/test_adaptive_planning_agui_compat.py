"""AG-UI compatibility tests for no-op PlanningStep produced by adaptive_planning.

Verifies that the no-op PlanningStep yielded when agent is on_track:
  - serialises with .dict()
  - is isinstance(PlanningStep)
  - can be appended to AgentMemory.steps without error
  - to_messages() works
"""
import time
from unittest.mock import MagicMock, patch

import pytest

from smolagents.agents import MultiStepAgent
from smolagents.memory import ActionStep, AgentMemory, PlanningStep
from smolagents.models import ChatMessage, MessageRole
from smolagents.monitoring import Timing, TokenUsage

from adaptive_planning import (
    AdaptivePlanningMixin,
    AdaptivePlanningToolCallingAgent,
    DEFAULT_FORCE_REPLAN_EVERY,
    Verdict,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_timing():
    t = time.time()
    return Timing(start_time=t, end_time=t)


def _make_planning_step(plan="The plan"):
    return PlanningStep(
        model_input_messages=[],
        model_output_message=ChatMessage(role=MessageRole.ASSISTANT, content=plan),
        plan=plan,
        timing=_make_timing(),
    )


def _get_noop_step(plan_text="My plan"):
    """Create an agent in on_track state and collect the no-op PlanningStep."""
    mock_model = MagicMock()
    mock_model.model_id = "test-model"
    agent = AdaptivePlanningToolCallingAgent(
        tools=[], model=mock_model, planning_interval=2
    )
    agent._adaptive_checks_since_replan = 0
    agent._adaptive_force_every = DEFAULT_FORCE_REPLAN_EVERY

    ps = _make_planning_step(plan_text)
    agent.memory.steps = [ps]

    monitor_resp = MagicMock()
    monitor_resp.content = '{"on_track": true, "reason": "fine"}'
    monitor_resp.token_usage = None
    monitor = MagicMock()
    monitor.generate.return_value = monitor_resp
    agent._monitor_model = monitor

    def fake_super(self, task, is_first_step, step):
        yield _make_planning_step("fallback")

    with patch.object(MultiStepAgent, "_generate_planning_step", fake_super):
        steps = list(agent._generate_planning_step("task", is_first_step=False, step=2))

    assert len(steps) == 1
    return steps[0]


# ---------------------------------------------------------------------------
# isinstance check
# ---------------------------------------------------------------------------

def test_noop_step_is_planning_step():
    step = _get_noop_step()
    assert isinstance(step, PlanningStep)


# ---------------------------------------------------------------------------
# .dict() serialisation
# ---------------------------------------------------------------------------

def test_noop_step_dict_serialises():
    step = _get_noop_step()
    d = step.dict()
    assert isinstance(d, dict)


def test_noop_step_dict_has_plan_key():
    step = _get_noop_step("Serialise me")
    d = step.dict()
    assert "plan" in d
    assert d["plan"] == "Serialise me"


def test_noop_step_dict_has_model_input_messages():
    step = _get_noop_step()
    d = step.dict()
    assert "model_input_messages" in d
    assert d["model_input_messages"] == []


def test_noop_step_dict_has_timing_key():
    step = _get_noop_step()
    d = step.dict()
    assert "timing" in d


def test_noop_step_dict_has_token_usage_key():
    step = _get_noop_step()
    d = step.dict()
    assert "token_usage" in d


# ---------------------------------------------------------------------------
# AgentMemory.steps.append does not raise
# ---------------------------------------------------------------------------

def test_noop_step_can_be_appended_to_memory_steps():
    mem = AgentMemory(system_prompt="test")
    step = _get_noop_step()
    mem.steps.append(step)
    assert len(mem.steps) == 1
    assert mem.steps[0] is step


def test_noop_step_memory_steps_append_multiple():
    mem = AgentMemory(system_prompt="test")
    for i in range(3):
        step = _get_noop_step(f"plan {i}")
        mem.steps.append(step)
    assert len(mem.steps) == 3


# ---------------------------------------------------------------------------
# to_messages() works
# ---------------------------------------------------------------------------

def test_noop_step_to_messages_does_not_raise():
    step = _get_noop_step()
    msgs = step.to_messages(summary_mode=False)
    assert msgs is not None


def test_noop_step_to_messages_returns_list():
    step = _get_noop_step()
    msgs = step.to_messages(summary_mode=False)
    assert isinstance(msgs, list)


def test_noop_step_to_messages_summary_mode():
    step = _get_noop_step()
    msgs = step.to_messages(summary_mode=True)
    assert isinstance(msgs, list)


# ---------------------------------------------------------------------------
# model_output_message contains adaptive-monitor annotation
# ---------------------------------------------------------------------------

def test_noop_step_output_message_contains_on_track():
    step = _get_noop_step()
    content = step.model_output_message.content
    assert "on_track" in content.lower() or "adaptive-monitor" in content


# ---------------------------------------------------------------------------
# _make_noop_planning_step directly
# ---------------------------------------------------------------------------

def test_make_noop_planning_step_directly():
    """Test AdaptivePlanningMixin._make_noop_planning_step directly."""
    mock_model = MagicMock()
    mock_model.model_id = "test-model"
    agent = AdaptivePlanningToolCallingAgent(
        tools=[], model=mock_model, planning_interval=2
    )

    verdict = Verdict(on_track=True, replan_needed=False, reason="looks fine", token_usage=None)
    start_time = time.time()
    step = agent._make_noop_planning_step("the current plan", verdict, start_time)

    assert isinstance(step, PlanningStep)
    assert step.plan == "the current plan"
    assert step.model_input_messages == []


def test_make_noop_planning_step_with_token_usage():
    mock_model = MagicMock()
    mock_model.model_id = "test-model"
    agent = AdaptivePlanningToolCallingAgent(
        tools=[], model=mock_model, planning_interval=2
    )

    usage = TokenUsage(input_tokens=5, output_tokens=3)
    verdict = Verdict(on_track=True, replan_needed=False, reason="ok", token_usage=usage)
    step = agent._make_noop_planning_step("plan", verdict, time.time())

    assert step.token_usage is usage


def test_make_noop_planning_step_timing_set():
    mock_model = MagicMock()
    mock_model.model_id = "test-model"
    agent = AdaptivePlanningToolCallingAgent(
        tools=[], model=mock_model, planning_interval=2
    )

    verdict = Verdict(on_track=True, replan_needed=False, reason="ok")
    start_time = time.time()
    step = agent._make_noop_planning_step("plan", verdict, start_time)

    assert step.timing is not None
    assert step.timing.start_time == start_time
    assert step.timing.end_time >= start_time
