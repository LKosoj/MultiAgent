"""WS-A / M-4: post_step_judge не применяется к детерминированным tool-шагам.

LLM-судья валидирует output против text-плана и на tool-шагах (валидный dict/путь)
даёт ложные RETRY/STOP. Guard: судим только НЕ-tool (агентные) шаги.

Проверяем:
  * step_type="tool" + post_step_judge включён → judge.validate_result НЕ вызывается,
    шаг завершается COMPLETED без judge-вердикта;
  * step_type="agent" + post_step_judge включён → judge вызывается ровно один раз.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

_LIGHT_WORKFLOW_MODULES = [
    "workflow",
    "workflow.engine",
    "workflow.models",
    "workflow.state_manager",
    "workflow.retry_engine",
    "workflow.resource_manager",
    "agent_system",
]
_MISSING_MODULE = object()


@pytest.fixture(autouse=True)
def _restore_light_workflow_modules():
    saved = {name: sys.modules.get(name, _MISSING_MODULE) for name in _LIGHT_WORKFLOW_MODULES}
    yield
    for name, module in saved.items():
        if module is _MISSING_MODULE:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


def _load_module(module_name: str, file_path: Path):
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Failed to load module spec for {module_name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _install_light_workflow_package():
    for module_name in _LIGHT_WORKFLOW_MODULES:
        sys.modules.pop(module_name, None)

    workflow_pkg = types.ModuleType("workflow")
    workflow_pkg.__path__ = [str(ROOT / "workflow")]
    workflow_pkg.__lightweight__ = True
    sys.modules["workflow"] = workflow_pkg

    agent_system = types.ModuleType("agent_system")

    class DynamicAgentSystem:
        pass

    agent_system.DynamicAgentSystem = DynamicAgentSystem
    sys.modules["agent_system"] = agent_system

    for module_name in [
        "workflow.models",
        "workflow.state_manager",
        "workflow.retry_engine",
        "workflow.resource_manager",
    ]:
        relative_path = module_name.split(".", 1)[1].replace(".", "/") + ".py"
        module = _load_module(module_name, ROOT / "workflow" / relative_path)
        setattr(workflow_pkg, module_name.rsplit(".", 1)[1], module)

    engine_module = _load_module("workflow.engine", ROOT / "workflow" / "engine.py")
    workflow_pkg.engine = engine_module
    return workflow_pkg


def _enhanced_engine_instance():
    pkg = _install_light_workflow_package()
    enhanced_module = _load_module(
        "workflow.enhanced_engine",
        ROOT / "workflow" / "enhanced_engine.py",
    )
    pkg.enhanced_engine = enhanced_module
    return object.__new__(enhanced_module.EnhancedWorkflowEngine)


def _models():
    return sys.modules["workflow.models"]


class _RecordingJudge:
    def __init__(self):
        self.calls = 0

    async def validate_result(self, step_result, plan, step):
        self.calls += 1
        return types.SimpleNamespace(
            overall_score=0.95,
            validation_passed=True,
            validator_results=[],
        )


def _install_single_attempt_stubs(engine, models, judge, feature_on=True):
    """Стабы для _execute_single_step_attempt: circuit breaker возвращает COMPLETED,
    бюджет — no-op, judge/decision — записываемые. post_step_judge включён."""
    completed = models.StepResult(
        step_id="_",
        status=models.StepStatus.COMPLETED,
        output={"path": "/out/final.mp4"},
        start_time=datetime.now(),
        end_time=datetime.now(),
    )

    async def call_agent_safely(**kwargs):
        completed.step_id = kwargs["step"].id
        return completed

    engine.circuit_breaker_manager = types.SimpleNamespace(call_agent_safely=call_agent_safely)
    engine.budget_manager = types.SimpleNamespace(consume_budget=lambda *a, **k: None)
    # pre_step_planner выключен, post_step_judge включён.
    engine.feature_manager = types.SimpleNamespace(
        is_feature_enabled=lambda feature, *a, **k: (feature == "post_step_judge") and feature_on,
    )
    engine.judge = judge

    async def make_decision(*args, **kwargs):
        return types.SimpleNamespace(action="proceed", reason="ok")

    engine.decision_engine = types.SimpleNamespace(make_decision=make_decision)


def _run_attempt(engine, models, step):
    ctx = models.WorkflowContext(workflow_id="wf-judge", session_id="sess-judge")
    retry_context = {
        "step": step,
        "workflow_context": ctx,
        "previous_results": {},
        "step_budget": object(),
    }
    return asyncio.run(engine._execute_single_step_attempt(retry_context))


# ---------------------------------------------------------------------------
# 1. tool-шаг: judge НЕ вызывается
# ---------------------------------------------------------------------------
def test_judge_not_called_for_tool_step():
    engine = _enhanced_engine_instance()
    models = _models()
    judge = _RecordingJudge()
    _install_single_attempt_stubs(engine, models, judge)

    step = models.WorkflowStep(id="video_generator", task="gen", step_type="tool",
                               tool_name="video_generator_aitunnel_tool")

    result = _run_attempt(engine, models, step)

    assert judge.calls == 0, "judge не должен применяться к детерминированному tool-шагу"
    assert result.status == models.StepStatus.COMPLETED


# ---------------------------------------------------------------------------
# 2. agent-шаг: judge вызывается один раз
# ---------------------------------------------------------------------------
def test_judge_called_for_agent_step():
    engine = _enhanced_engine_instance()
    models = _models()
    judge = _RecordingJudge()
    _install_single_attempt_stubs(engine, models, judge)

    step = models.WorkflowStep(id="story_writer", task="write", step_type="agent",
                               agent_type="story_writer_agent")

    result = _run_attempt(engine, models, step)

    assert judge.calls == 1, "judge должен валидировать агентный шаг"
    assert result.status == models.StepStatus.COMPLETED
