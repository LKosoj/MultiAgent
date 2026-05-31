"""Backward-compat: int/None planning_interval должен выбирать обычный
smolagents-класс без мониторных атрибутов. Адаптивный режим — строго opt-in.

Здесь проверяется ИМЕННО решение, которое принимает agent_factory (выбор класса
+ маршрутизация smol_interval), без тяжёлой сборки агента через фабрику.
"""
from unittest.mock import MagicMock

import pytest

from smolagents import CodeAgent, ToolCallingAgent

from adaptive_planning import (
    AdaptivePlanningCodeAgent,
    AdaptivePlanningMixin,
    AdaptivePlanningToolCallingAgent,
    normalize_planning_interval,
)


def _select_tool_calling_cls(value):
    """Реплика решения agent_factory для tool_calling-ветки."""
    cfg = normalize_planning_interval(value)
    return AdaptivePlanningToolCallingAgent if cfg.adaptive else ToolCallingAgent


def _select_code_cls(value):
    """Реплика решения agent_factory для code-ветки."""
    cfg = normalize_planning_interval(value)
    return AdaptivePlanningCodeAgent if cfg.adaptive else CodeAgent


@pytest.mark.parametrize("value", [None, 0, 1, 2, 5, "2"])
def test_non_adaptive_values_select_plain_classes(value):
    assert _select_tool_calling_cls(value) is ToolCallingAgent
    assert _select_code_cls(value) is CodeAgent


@pytest.mark.parametrize("value", ["adaptive", "adaptive:3"])
def test_adaptive_values_select_adaptive_classes(value):
    assert _select_tool_calling_cls(value) is AdaptivePlanningToolCallingAgent
    assert _select_code_cls(value) is AdaptivePlanningCodeAgent


def test_plain_classes_have_no_mixin():
    assert not issubclass(ToolCallingAgent, AdaptivePlanningMixin)
    assert not issubclass(CodeAgent, AdaptivePlanningMixin)


def test_adaptive_classes_have_mixin_and_base():
    assert issubclass(AdaptivePlanningToolCallingAgent, AdaptivePlanningMixin)
    assert issubclass(AdaptivePlanningToolCallingAgent, ToolCallingAgent)
    assert issubclass(AdaptivePlanningCodeAgent, AdaptivePlanningMixin)
    assert issubclass(AdaptivePlanningCodeAgent, CodeAgent)


def test_plain_agent_instance_has_no_adaptive_attrs():
    """Обычный агент (int-путь) не получает мониторных атрибутов."""
    mock_model = MagicMock()
    mock_model.model_id = "test-model"
    agent = ToolCallingAgent(tools=[], model=mock_model, planning_interval=2)
    assert not hasattr(agent, "_monitor_model")
    assert not hasattr(agent, "_adaptive_force_every")
    assert not hasattr(agent, "_adaptive_checks_since_replan")


def test_non_adaptive_smol_interval_passthrough():
    """int/None уходит в smolagents без изменений — триггер планирования прежний."""
    assert normalize_planning_interval(2).smol_interval == 2
    assert normalize_planning_interval(None).smol_interval is None
    assert normalize_planning_interval(0).smol_interval is None
    # адаптивный режим всё равно даёт целочисленный каданс (никогда не None),
    # иначе smolagents-триггер (step % interval) сломался бы.
    assert normalize_planning_interval("adaptive").smol_interval == 2
