"""Tests for AdaptivePlanningMixin._generate_planning_step — all branches."""
import time
from unittest.mock import MagicMock, patch

import pytest

from smolagents.agents import MultiStepAgent
from smolagents.memory import ActionStep, AgentMemory, PlanningStep
from smolagents.models import ChatMessage, MessageRole
from smolagents.monitoring import Timing, TokenUsage

from adaptive_planning import (
    AdaptivePlanningCodeAgent,
    AdaptivePlanningToolCallingAgent,
    DEFAULT_FORCE_REPLAN_EVERY,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_timing():
    t = time.time()
    return Timing(start_time=t, end_time=t)


def _make_planning_step(plan="My plan"):
    return PlanningStep(
        model_input_messages=[],
        model_output_message=ChatMessage(role=MessageRole.ASSISTANT, content=plan),
        plan=plan,
        timing=_make_timing(),
    )


def _make_action_step(step_number=1, error=None, observations=None):
    return ActionStep(
        step_number=step_number,
        timing=_make_timing(),
        error=error,
        observations=observations,
    )


def _make_new_plan_step(plan="New plan"):
    return _make_planning_step(plan)


def _make_agent():
    """Create AdaptivePlanningToolCallingAgent with mock model."""
    mock_model = MagicMock()
    mock_model.model_id = "test-model"
    agent = AdaptivePlanningToolCallingAgent(
        tools=[], model=mock_model, planning_interval=2
    )
    return agent


def _super_gen_factory(plan="Super plan"):
    """Return a fake super()._generate_planning_step generator."""
    def _gen(self, task, is_first_step, step):
        yield _make_new_plan_step(plan)
    return _gen


# ---------------------------------------------------------------------------
# is_first_step = True  →  delegate to super, reset counter
# ---------------------------------------------------------------------------

def test_first_step_delegates_to_super():
    agent = _make_agent()
    agent._adaptive_checks_since_replan = 3  # should be reset

    super_called = []

    def fake_super(self, task, is_first_step, step):
        super_called.append(True)
        yield _make_new_plan_step()

    with patch.object(MultiStepAgent, "_generate_planning_step", fake_super):
        steps = list(agent._generate_planning_step("task", is_first_step=True, step=1))

    assert len(super_called) == 1
    assert agent._adaptive_checks_since_replan == 0


def test_first_step_counter_reset_to_zero():
    agent = _make_agent()
    agent._adaptive_checks_since_replan = 99

    def fake_super(self, task, is_first_step, step):
        yield _make_new_plan_step()

    with patch.object(MultiStepAgent, "_generate_planning_step", fake_super):
        list(agent._generate_planning_step("task", is_first_step=True, step=1))

    assert agent._adaptive_checks_since_replan == 0


def test_first_step_yields_super_result():
    agent = _make_agent()

    def fake_super(self, task, is_first_step, step):
        yield _make_new_plan_step("First plan")

    with patch.object(MultiStepAgent, "_generate_planning_step", fake_super):
        steps = list(agent._generate_planning_step("task", is_first_step=True, step=1))

    assert len(steps) == 1
    assert steps[0].plan == "First plan"


# ---------------------------------------------------------------------------
# Error in action window  →  delegate to super, reset counter
# ---------------------------------------------------------------------------

def test_error_in_window_triggers_super():
    agent = _make_agent()
    agent._monitor_model = MagicMock()
    agent._adaptive_force_every = DEFAULT_FORCE_REPLAN_EVERY
    agent._adaptive_checks_since_replan = 1

    ps = _make_planning_step("My plan")
    a_err = _make_action_step(step_number=1, error="boom")
    agent.memory.steps = [ps, a_err]

    super_called = []

    def fake_super(self, task, is_first_step, step):
        super_called.append(True)
        yield _make_new_plan_step()

    with patch.object(MultiStepAgent, "_generate_planning_step", fake_super):
        list(agent._generate_planning_step("task", is_first_step=False, step=2))

    assert len(super_called) == 1
    assert agent._adaptive_checks_since_replan == 0


def test_error_in_window_counter_reset():
    agent = _make_agent()
    agent._monitor_model = MagicMock()
    agent._adaptive_force_every = DEFAULT_FORCE_REPLAN_EVERY
    agent._adaptive_checks_since_replan = 2

    ps = _make_planning_step()
    a_err = _make_action_step(error="network timeout")
    agent.memory.steps = [ps, a_err]

    def fake_super(self, task, is_first_step, step):
        yield _make_new_plan_step()

    with patch.object(MultiStepAgent, "_generate_planning_step", fake_super):
        list(agent._generate_planning_step("task", is_first_step=False, step=2))

    assert agent._adaptive_checks_since_replan == 0


def test_no_error_in_window_does_not_force_super_via_error():
    """Action step without error should not trigger error path."""
    agent = _make_agent()
    agent._adaptive_checks_since_replan = 0
    agent._adaptive_force_every = DEFAULT_FORCE_REPLAN_EVERY

    ps = _make_planning_step("My plan")
    ok_step = _make_action_step(step_number=1, error=None)
    agent.memory.steps = [ps, ok_step]

    # Set up monitor to return on_track so we can verify super NOT called via error path
    monitor_resp = MagicMock()
    monitor_resp.content = '{"on_track": true, "reason": "ok"}'
    monitor_resp.token_usage = None
    monitor = MagicMock()
    monitor.generate.return_value = monitor_resp
    agent._monitor_model = monitor

    super_called = []

    def fake_super(self, task, is_first_step, step):
        super_called.append(True)
        yield _make_new_plan_step()

    with patch.object(MultiStepAgent, "_generate_planning_step", fake_super):
        list(agent._generate_planning_step("task", is_first_step=False, step=2))

    assert len(super_called) == 0  # monitor said on_track


# ---------------------------------------------------------------------------
# Force replan (checks >= force_every)
# ---------------------------------------------------------------------------

def test_force_replan_at_threshold():
    agent = _make_agent()
    agent._monitor_model = MagicMock()
    agent._adaptive_force_every = 3
    agent._adaptive_checks_since_replan = 3  # >= force_every

    ps = _make_planning_step()
    agent.memory.steps = [ps]

    super_called = []

    def fake_super(self, task, is_first_step, step):
        super_called.append(True)
        yield _make_new_plan_step()

    with patch.object(MultiStepAgent, "_generate_planning_step", fake_super):
        list(agent._generate_planning_step("task", is_first_step=False, step=3))

    assert len(super_called) == 1
    assert agent._adaptive_checks_since_replan == 0


def test_force_replan_counter_reset():
    agent = _make_agent()
    agent._monitor_model = MagicMock()
    agent._adaptive_force_every = 2
    agent._adaptive_checks_since_replan = 5

    ps = _make_planning_step()
    agent.memory.steps = [ps]

    def fake_super(self, task, is_first_step, step):
        yield _make_new_plan_step()

    with patch.object(MultiStepAgent, "_generate_planning_step", fake_super):
        list(agent._generate_planning_step("task", is_first_step=False, step=3))

    assert agent._adaptive_checks_since_replan == 0


def test_below_force_threshold_not_forced():
    """checks < force_every and monitor says on_track → should yield no-op."""
    agent = _make_agent()
    agent._adaptive_force_every = 4
    agent._adaptive_checks_since_replan = 2  # < 4

    ps = _make_planning_step("My plan")
    agent.memory.steps = [ps]

    monitor_resp = MagicMock()
    monitor_resp.content = '{"on_track": true, "reason": "ok"}'
    monitor_resp.token_usage = None
    monitor = MagicMock()
    monitor.generate.return_value = monitor_resp
    agent._monitor_model = monitor

    def fake_super(self, task, is_first_step, step):
        yield _make_new_plan_step()

    super_called = []

    def counting_super(self, task, is_first_step, step):
        super_called.append(True)
        yield _make_new_plan_step()

    with patch.object(MultiStepAgent, "_generate_planning_step", counting_super):
        steps = list(agent._generate_planning_step("task", is_first_step=False, step=3))

    assert len(super_called) == 0
    assert len(steps) == 1  # no-op step


# ---------------------------------------------------------------------------
# monitor_model is None  →  failsafe super, reset counter
# ---------------------------------------------------------------------------

def test_no_monitor_model_delegates_to_super():
    agent = _make_agent()
    agent._monitor_model = None
    agent._adaptive_force_every = DEFAULT_FORCE_REPLAN_EVERY
    agent._adaptive_checks_since_replan = 1

    ps = _make_planning_step()
    agent.memory.steps = [ps]

    super_called = []

    def fake_super(self, task, is_first_step, step):
        super_called.append(True)
        yield _make_new_plan_step()

    with patch.object(MultiStepAgent, "_generate_planning_step", fake_super):
        list(agent._generate_planning_step("task", is_first_step=False, step=2))

    assert len(super_called) == 1
    assert agent._adaptive_checks_since_replan == 0


def test_no_monitor_model_attribute_defaults_to_none():
    """If _monitor_model is not set at all, should behave as None."""
    agent = _make_agent()
    # Don't set _monitor_model at all
    if hasattr(agent, "_monitor_model"):
        del agent._monitor_model
    agent._adaptive_force_every = DEFAULT_FORCE_REPLAN_EVERY
    agent._adaptive_checks_since_replan = 0

    ps = _make_planning_step()
    agent.memory.steps = [ps]

    super_called = []

    def fake_super(self, task, is_first_step, step):
        super_called.append(True)
        yield _make_new_plan_step()

    with patch.object(MultiStepAgent, "_generate_planning_step", fake_super):
        list(agent._generate_planning_step("task", is_first_step=False, step=2))

    assert len(super_called) == 1


# ---------------------------------------------------------------------------
# on_track → yield no-op PlanningStep, increment counter
# ---------------------------------------------------------------------------

def test_on_track_yields_noop_planning_step():
    agent = _make_agent()
    agent._adaptive_checks_since_replan = 0
    agent._adaptive_force_every = DEFAULT_FORCE_REPLAN_EVERY

    ps = _make_planning_step("Current plan")
    agent.memory.steps = [ps]

    monitor_resp = MagicMock()
    monitor_resp.content = '{"on_track": true, "reason": "fine"}'
    monitor_resp.token_usage = None
    monitor = MagicMock()
    monitor.generate.return_value = monitor_resp
    agent._monitor_model = monitor

    def fake_super(self, task, is_first_step, step):
        yield _make_new_plan_step()

    with patch.object(MultiStepAgent, "_generate_planning_step", fake_super):
        steps = list(agent._generate_planning_step("task", is_first_step=False, step=2))

    assert len(steps) == 1
    assert isinstance(steps[0], PlanningStep)


def test_on_track_noop_step_plan_equals_current_plan():
    agent = _make_agent()
    agent._adaptive_checks_since_replan = 0
    agent._adaptive_force_every = DEFAULT_FORCE_REPLAN_EVERY

    ps = _make_planning_step("Step 1: do A, Step 2: do B")
    agent.memory.steps = [ps]

    monitor_resp = MagicMock()
    monitor_resp.content = '{"on_track": true, "reason": "fine"}'
    monitor_resp.token_usage = None
    monitor = MagicMock()
    monitor.generate.return_value = monitor_resp
    agent._monitor_model = monitor

    def fake_super(self, task, is_first_step, step):
        yield _make_new_plan_step("something else")

    with patch.object(MultiStepAgent, "_generate_planning_step", fake_super):
        steps = list(agent._generate_planning_step("task", is_first_step=False, step=2))

    assert steps[0].plan == "Step 1: do A, Step 2: do B"


def test_on_track_noop_step_model_input_messages_empty():
    agent = _make_agent()
    agent._adaptive_checks_since_replan = 0
    agent._adaptive_force_every = DEFAULT_FORCE_REPLAN_EVERY

    ps = _make_planning_step("My plan")
    agent.memory.steps = [ps]

    monitor_resp = MagicMock()
    monitor_resp.content = '{"on_track": true, "reason": "ok"}'
    monitor_resp.token_usage = None
    monitor = MagicMock()
    monitor.generate.return_value = monitor_resp
    agent._monitor_model = monitor

    def fake_super(self, task, is_first_step, step):
        yield _make_new_plan_step()

    with patch.object(MultiStepAgent, "_generate_planning_step", fake_super):
        steps = list(agent._generate_planning_step("task", is_first_step=False, step=2))

    assert steps[0].model_input_messages == []


def test_on_track_counter_incremented():
    agent = _make_agent()
    agent._adaptive_checks_since_replan = 1
    agent._adaptive_force_every = DEFAULT_FORCE_REPLAN_EVERY

    ps = _make_planning_step("My plan")
    agent.memory.steps = [ps]

    monitor_resp = MagicMock()
    monitor_resp.content = '{"on_track": true, "reason": "ok"}'
    monitor_resp.token_usage = None
    monitor = MagicMock()
    monitor.generate.return_value = monitor_resp
    agent._monitor_model = monitor

    def fake_super(self, task, is_first_step, step):
        yield _make_new_plan_step()

    with patch.object(MultiStepAgent, "_generate_planning_step", fake_super):
        list(agent._generate_planning_step("task", is_first_step=False, step=2))

    assert agent._adaptive_checks_since_replan == 2  # was 1, now 2


# ---------------------------------------------------------------------------
# replan_needed → delegate to super, reset counter
# ---------------------------------------------------------------------------

def test_replan_needed_delegates_to_super():
    agent = _make_agent()
    agent._adaptive_checks_since_replan = 2
    agent._adaptive_force_every = DEFAULT_FORCE_REPLAN_EVERY

    ps = _make_planning_step("My plan")
    agent.memory.steps = [ps]

    monitor_resp = MagicMock()
    monitor_resp.content = '{"on_track": false, "reason": "error"}'
    monitor_resp.token_usage = None
    monitor = MagicMock()
    monitor.generate.return_value = monitor_resp
    agent._monitor_model = monitor

    super_called = []

    def fake_super(self, task, is_first_step, step):
        super_called.append(True)
        yield _make_new_plan_step("New plan after replan")

    with patch.object(MultiStepAgent, "_generate_planning_step", fake_super):
        steps = list(agent._generate_planning_step("task", is_first_step=False, step=2))

    assert len(super_called) == 1
    assert agent._adaptive_checks_since_replan == 0


def test_replan_needed_counter_reset():
    agent = _make_agent()
    agent._adaptive_checks_since_replan = 3
    agent._adaptive_force_every = DEFAULT_FORCE_REPLAN_EVERY

    ps = _make_planning_step("My plan")
    agent.memory.steps = [ps]

    monitor_resp = MagicMock()
    monitor_resp.content = '{"on_track": false, "reason": "deviated"}'
    monitor_resp.token_usage = None
    monitor = MagicMock()
    monitor.generate.return_value = monitor_resp
    agent._monitor_model = monitor

    def fake_super(self, task, is_first_step, step):
        yield _make_new_plan_step()

    with patch.object(MultiStepAgent, "_generate_planning_step", fake_super):
        list(agent._generate_planning_step("task", is_first_step=False, step=2))

    assert agent._adaptive_checks_since_replan == 0


# ---------------------------------------------------------------------------
# Sequence: N on_track checks accumulate, force_every triggers replan
# ---------------------------------------------------------------------------

def test_on_track_sequence_triggers_forced_replan_at_threshold():
    """force_every подряд on_track копят счётчик (все no-op), а следующий вызов
    (checks == force_every) принудительно перепланирует и сбрасывает счётчик."""
    agent = _make_agent()
    force_every = 4
    agent._adaptive_force_every = force_every
    agent._adaptive_checks_since_replan = 0

    ps = _make_planning_step("Plan")
    agent.memory.steps = [ps]

    monitor_resp = MagicMock()
    monitor_resp.content = '{"on_track": true, "reason": "ok"}'
    monitor_resp.token_usage = None
    monitor = MagicMock()
    monitor.generate.return_value = monitor_resp
    agent._monitor_model = monitor

    super_calls = []

    def fake_super(self, task, is_first_step, step):
        super_calls.append(True)
        yield _make_new_plan_step()

    with patch.object(MultiStepAgent, "_generate_planning_step", fake_super):
        for i in range(force_every):
            steps = list(
                agent._generate_planning_step("task", is_first_step=False, step=i + 2)
            )
            assert len(super_calls) == 0  # пока что только no-op
            assert isinstance(steps[0], PlanningStep)
        assert agent._adaptive_checks_since_replan == force_every

        # checks == force_every → принудительный replan
        list(
            agent._generate_planning_step(
                "task", is_first_step=False, step=force_every + 2
            )
        )

    assert len(super_calls) == 1
    assert agent._adaptive_checks_since_replan == 0


# ---------------------------------------------------------------------------
# AdaptivePlanningToolCallingAgent subclass — smoke test
# ---------------------------------------------------------------------------

def test_tool_calling_agent_on_track():
    mock_model = MagicMock()
    mock_model.model_id = "test-model"
    agent = AdaptivePlanningToolCallingAgent(
        tools=[], model=mock_model, planning_interval=2
    )
    agent._adaptive_checks_since_replan = 0
    agent._adaptive_force_every = DEFAULT_FORCE_REPLAN_EVERY

    ps = _make_planning_step("Tool plan")
    agent.memory.steps = [ps]

    monitor_resp = MagicMock()
    monitor_resp.content = '{"on_track": true, "reason": "ok"}'
    monitor_resp.token_usage = None
    monitor = MagicMock()
    monitor.generate.return_value = monitor_resp
    agent._monitor_model = monitor

    def fake_super(self, task, is_first_step, step):
        yield _make_new_plan_step()

    with patch.object(MultiStepAgent, "_generate_planning_step", fake_super):
        steps = list(agent._generate_planning_step("task", is_first_step=False, step=2))

    assert isinstance(steps[0], PlanningStep)
    assert steps[0].plan == "Tool plan"


# ---------------------------------------------------------------------------
# evaluate_plan_adherence called with correct context
# ---------------------------------------------------------------------------

def test_monitor_called_with_current_plan_in_context():
    agent = _make_agent()
    agent._adaptive_checks_since_replan = 0
    agent._adaptive_force_every = DEFAULT_FORCE_REPLAN_EVERY

    ps = _make_planning_step("The current plan text")
    agent.memory.steps = [ps]

    monitor_resp = MagicMock()
    monitor_resp.content = '{"on_track": true, "reason": "ok"}'
    monitor_resp.token_usage = None
    monitor = MagicMock()
    monitor.generate.return_value = monitor_resp
    agent._monitor_model = monitor

    def fake_super(self, task, is_first_step, step):
        yield _make_new_plan_step()

    called_with = []

    def fake_evaluate(model, task, current_plan, recent_context):
        called_with.append(current_plan)
        from adaptive_planning import Verdict
        return Verdict(True, False, "ok")

    with patch.object(MultiStepAgent, "_generate_planning_step", fake_super):
        with patch("adaptive_planning.evaluate_plan_adherence", fake_evaluate):
            list(agent._generate_planning_step("task", is_first_step=False, step=2))

    assert len(called_with) == 1
    assert called_with[0] == "The current plan text"


# ---------------------------------------------------------------------------
# _adaptive_checks_since_replan defaults to 0 via getattr
# ---------------------------------------------------------------------------

def test_counter_defaults_to_zero_when_attribute_missing():
    """Counter should default to 0 even if never set."""
    agent = _make_agent()
    agent._adaptive_force_every = DEFAULT_FORCE_REPLAN_EVERY
    # Deliberately do NOT set _adaptive_checks_since_replan

    ps = _make_planning_step("My plan")
    agent.memory.steps = [ps]

    monitor_resp = MagicMock()
    monitor_resp.content = '{"on_track": true, "reason": "ok"}'
    monitor_resp.token_usage = None
    monitor = MagicMock()
    monitor.generate.return_value = monitor_resp
    agent._monitor_model = monitor

    def fake_super(self, task, is_first_step, step):
        yield _make_new_plan_step()

    with patch.object(MultiStepAgent, "_generate_planning_step", fake_super):
        steps = list(agent._generate_planning_step("task", is_first_step=False, step=2))

    # Should have incremented from 0 to 1
    assert agent._adaptive_checks_since_replan == 1
