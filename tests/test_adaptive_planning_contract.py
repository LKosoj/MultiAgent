"""Contract-pin tests for adaptive_planning.

Pins the smolagents API surface that adaptive_planning.py depends on,
so a smolagents upgrade breaking the contract will be detected immediately.
"""
import inspect

import pytest

from smolagents.agents import MultiStepAgent
from smolagents.memory import ActionStep, PlanningStep
from smolagents.monitoring import Timing, TokenUsage
from smolagents.models import ChatMessage, MessageRole
import time


# ---------------------------------------------------------------------------
# MultiStepAgent._generate_planning_step signature
# ---------------------------------------------------------------------------

def test_generate_planning_step_param_names():
    sig = inspect.signature(MultiStepAgent._generate_planning_step)
    assert list(sig.parameters.keys()) == ["self", "task", "is_first_step", "step"]


def test_generate_planning_step_is_first_step_annotation():
    sig = inspect.signature(MultiStepAgent._generate_planning_step)
    assert sig.parameters["is_first_step"].annotation is bool


def test_generate_planning_step_step_annotation():
    sig = inspect.signature(MultiStepAgent._generate_planning_step)
    assert sig.parameters["step"].annotation is int


# ---------------------------------------------------------------------------
# PlanningStep fields
# ---------------------------------------------------------------------------

def test_planning_step_has_plan_field():
    import dataclasses
    names = [f.name for f in dataclasses.fields(PlanningStep)]
    assert "plan" in names


def test_planning_step_has_model_input_messages_field():
    import dataclasses
    names = [f.name for f in dataclasses.fields(PlanningStep)]
    assert "model_input_messages" in names


def test_planning_step_has_timing_field():
    import dataclasses
    names = [f.name for f in dataclasses.fields(PlanningStep)]
    assert "timing" in names


def test_planning_step_has_token_usage_field():
    import dataclasses
    names = [f.name for f in dataclasses.fields(PlanningStep)]
    assert "token_usage" in names


def test_planning_step_has_model_output_message_field():
    import dataclasses
    names = [f.name for f in dataclasses.fields(PlanningStep)]
    assert "model_output_message" in names


# ---------------------------------------------------------------------------
# ActionStep.error default is None
# ---------------------------------------------------------------------------

def test_action_step_error_default_none():
    import dataclasses
    fields_by_name = {f.name: f for f in dataclasses.fields(ActionStep)}
    assert "error" in fields_by_name
    assert fields_by_name["error"].default is None


# ---------------------------------------------------------------------------
# Planning trigger formula in _run_stream
# _generate_planning_step is called when:
#   planning_interval is not None
#   AND (step_number == 1 OR (step_number - 1) % planning_interval == 0)
# ---------------------------------------------------------------------------

def test_run_stream_contains_planning_interval_check():
    src = inspect.getsource(MultiStepAgent._run_stream)
    assert "planning_interval is not None" in src


def test_run_stream_contains_step_1_check():
    src = inspect.getsource(MultiStepAgent._run_stream)
    assert "step_number == 1" in src


def test_run_stream_contains_modulo_check():
    src = inspect.getsource(MultiStepAgent._run_stream)
    assert "planning_interval == 0" in src or "% self.planning_interval == 0" in src


def test_trigger_formula_interval_2_steps_1_3_5_7():
    """With interval=2, planning fires at steps 1,3,5,7 (step==1 OR (step-1)%2==0)."""
    def triggers(step_number, interval):
        return step_number == 1 or (step_number - 1) % interval == 0

    interval = 2
    assert triggers(1, interval) is True
    assert triggers(2, interval) is False
    assert triggers(3, interval) is True
    assert triggers(4, interval) is False
    assert triggers(5, interval) is True
    assert triggers(6, interval) is False
    assert triggers(7, interval) is True


def test_trigger_formula_interval_3():
    def triggers(step_number, interval):
        return step_number == 1 or (step_number - 1) % interval == 0

    interval = 3
    # step 1: always
    assert triggers(1, interval) is True
    # step 2,3: 1%3=1, 2%3=2 → no
    assert triggers(2, interval) is False
    assert triggers(3, interval) is False
    # step 4: 3%3=0 → yes
    assert triggers(4, interval) is True


# ---------------------------------------------------------------------------
# Timing requires start_time and end_time
# ---------------------------------------------------------------------------

def test_timing_constructor_requires_start_and_end():
    t = time.time()
    timing = Timing(start_time=t, end_time=t)
    assert timing.start_time == t
    assert timing.end_time == t


# ---------------------------------------------------------------------------
# TokenUsage constructor
# ---------------------------------------------------------------------------

def test_token_usage_constructor():
    tu = TokenUsage(input_tokens=100, output_tokens=50)
    assert tu.input_tokens == 100
    assert tu.output_tokens == 50


# ---------------------------------------------------------------------------
# PlanningStep is constructible with the fields adaptive_planning uses
# ---------------------------------------------------------------------------

def test_planning_step_constructible_with_empty_input_messages():
    t = time.time()
    ps = PlanningStep(
        model_input_messages=[],
        model_output_message=ChatMessage(role=MessageRole.ASSISTANT, content="note"),
        plan="the plan",
        timing=Timing(start_time=t, end_time=t),
        token_usage=None,
    )
    assert ps.plan == "the plan"
    assert ps.model_input_messages == []


# ---------------------------------------------------------------------------
# MRO contract: Mixin sits before CodeAgent and ToolCallingAgent
# ---------------------------------------------------------------------------

def test_mixin_before_code_agent_in_mro():
    from adaptive_planning import AdaptivePlanningCodeAgent, AdaptivePlanningMixin
    from smolagents import CodeAgent
    mro = AdaptivePlanningCodeAgent.__mro__
    mixin_idx = mro.index(AdaptivePlanningMixin)
    code_idx = mro.index(CodeAgent)
    assert mixin_idx < code_idx


def test_mixin_before_tool_calling_agent_in_mro():
    from adaptive_planning import AdaptivePlanningToolCallingAgent, AdaptivePlanningMixin
    from smolagents import ToolCallingAgent
    mro = AdaptivePlanningToolCallingAgent.__mro__
    mixin_idx = mro.index(AdaptivePlanningMixin)
    tool_idx = mro.index(ToolCallingAgent)
    assert mixin_idx < tool_idx


def test_generate_planning_step_defined_on_mixin():
    from adaptive_planning import AdaptivePlanningMixin
    assert "_generate_planning_step" in AdaptivePlanningMixin.__dict__
