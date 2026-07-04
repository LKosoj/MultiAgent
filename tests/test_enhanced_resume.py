"""WS-A / M-3: enhanced-resume для requires_enhanced_engine пайплайнов.

Проверяем:
  * execute_workflow(skip_steps=...) для requires_enhanced_engine НЕ бросает и НЕ
    деградирует в legacy, а исполняется enhanced-движком (пробрасывает
    skip_steps/restored_step_results в _execute_enhanced_workflow);
  * sequential enhanced-исполнитель пред-заполняет step_results восстановленными
    шагами и пропускает те, что в skip_steps (не запускает их повторно);
  * parallel enhanced-исполнитель прокидывает restored в initial_step_results —
    восстановленные шаги попадают в completed и не запускаются повторно;
  * финальный набор результатов содержит и восстановленные, и новые шаги (COMPLETED).
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


def _completed(models, step_id, output=None):
    return models.StepResult(
        step_id=step_id,
        status=models.StepStatus.COMPLETED,
        output=output if output is not None else {"ok": step_id},
        start_time=datetime.now(),
        end_time=datetime.now(),
    )


# ---------------------------------------------------------------------------
# 1. execute_workflow: required-enhanced resume идёт через enhanced (не raise/legacy)
# ---------------------------------------------------------------------------
def test_required_enhanced_resume_delegates_to_enhanced():
    engine = _enhanced_engine_instance()
    models = _models()

    workflow = models.WorkflowDefinition(
        name="requires-enhanced",
        steps=[],
        requires_enhanced_engine=True,
    )
    context = models.WorkflowContext(workflow_id="wf-resume", session_id="sess-resume")
    restored = {"s1": _completed(models, "s1")}
    captured = {}

    async def fake_enhanced(workflow_def, ctx, client_id=None, *, skip_steps=None,
                            restored_step_results=None):
        captured["skip_steps"] = skip_steps
        captured["restored"] = restored_step_results
        return models.WorkflowResult(
            workflow_id=ctx.workflow_id,
            status=models.WorkflowStatus.COMPLETED,
            start_time=datetime.now(),
        )

    engine._execute_enhanced_workflow = fake_enhanced

    result = asyncio.run(
        engine.execute_workflow(
            workflow, context, skip_steps={"s1"}, restored_step_results=restored,
        )
    )

    # Не бросил, вернул COMPLETED через enhanced-путь.
    assert result.status == models.WorkflowStatus.COMPLETED
    assert captured["skip_steps"] == {"s1"}
    assert captured["restored"] is restored


# ---------------------------------------------------------------------------
# 2. sequential: восстановленные шаги пред-заполнены и пропущены (не запущены)
# ---------------------------------------------------------------------------
def test_sequential_resume_skips_restored_and_prefills():
    engine = _enhanced_engine_instance()
    models = _models()

    s1 = models.WorkflowStep(id="s1", task="t1", agent_type="a")
    s2 = models.WorkflowStep(id="s2", task="t2", agent_type="a", depends_on=["s1"])
    s3 = models.WorkflowStep(id="s3", task="t3", agent_type="a", depends_on=["s2"])
    workflow = models.WorkflowDefinition(name="wf", steps=[s1, s2, s3], error_handling={})
    context = models.WorkflowContext(workflow_id="wf-seq", session_id="sess-seq")

    restored_s1 = _completed(models, "s1", output={"restored": True})
    executed = []

    async def fake_cancelled(workflow_id):
        return False

    async def fake_execute_step(step, ctx, previous):
        executed.append(step.id)
        return _completed(models, step.id)

    async def fake_complete(step, step_result, ctx, workflow_def, step_results):
        return step_result

    engine._is_workflow_cancelled = fake_cancelled
    engine._execute_enhanced_step = fake_execute_step
    engine._complete_enhanced_step_with_output_retry = fake_complete

    step_results = asyncio.run(
        engine._execute_enhanced_steps_sequential(
            workflow, context,
            skip_steps={"s1"},
            restored_step_results={"s1": restored_s1},
        )
    )

    # s1 НЕ исполнялся повторно; s2/s3 исполнены.
    assert executed == ["s2", "s3"]
    # Восстановленный результат s1 остался тем же объектом (пред-заполнение).
    assert step_results["s1"] is restored_s1
    assert set(step_results) == {"s1", "s2", "s3"}
    assert all(r.status == models.StepStatus.COMPLETED for r in step_results.values())


# ---------------------------------------------------------------------------
# 3. parallel: restored прокидывается в initial_step_results (не re-run)
# ---------------------------------------------------------------------------
def test_parallel_resume_via_initial_step_results():
    engine = _enhanced_engine_instance()
    models = _models()

    p1 = models.WorkflowStep(id="p1", task="t1", agent_type="a")
    p2 = models.WorkflowStep(id="p2", task="t2", agent_type="a", depends_on=["p1"])
    workflow = models.WorkflowDefinition(
        name="wf", steps=[p1, p2], parallel_execution=True, max_parallel_steps=2,
        error_handling={},
    )
    context = models.WorkflowContext(workflow_id="wf-par", session_id="sess-par")

    restored_p1 = _completed(models, "p1", output={"restored": True})
    executed = []

    async def fake_cancelled(workflow_id):
        return False

    async def fake_execute_step(step, ctx, previous):
        executed.append(step.id)
        return _completed(models, step.id)

    async def fake_complete(step, step_result, ctx, workflow_def, step_results):
        return step_result

    engine._is_workflow_cancelled = fake_cancelled
    engine._execute_enhanced_step = fake_execute_step
    engine._complete_enhanced_step_with_output_retry = fake_complete

    step_results = asyncio.run(
        engine._execute_enhanced_steps_parallel(
            workflow, context,
            restored_step_results={"p1": restored_p1},
        )
    )

    # p1 восстановлен → не запускался повторно; p2 запущен.
    assert executed == ["p2"]
    assert step_results["p1"] is restored_p1
    assert set(step_results) == {"p1", "p2"}
    assert all(r.status == models.StepStatus.COMPLETED for r in step_results.values())
