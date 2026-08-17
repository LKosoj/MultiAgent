"""WS-A / H-2 + M-1: retry-политика YAML уважается enhanced-движком.

Проверяем:
  * enhanced диспетчер читает step.retry_policy > global_retry_policy > дефолт
    и пробрасывает max_retries/base_delay/max_delay/retry_on_errors в retry-движок;
  * auto_retry_transient=false → max_retries=0;
  * metadata.retryable=false минует adaptive retry (прямой single-attempt);
  * AdaptiveRetryEngine с retry_on_errors прекращает попытки на нетранзиентной ошибке
    и повторяет транзиентную;
  * module-level classify_error классифицирует тексты таймаутов (в т.ч. русские).
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


class _CapturingRetryEngine:
    """Стаб retry-движка: захватывает kwargs, возвращает COMPLETED без вызова step_func."""

    def __init__(self, models):
        self.captured = None
        self._models = models

    async def execute_with_retry(self, *args, **kwargs):
        self.captured = kwargs
        m = self._models
        return m.StepResult(
            step_id=kwargs["step_id"],
            status=m.StepStatus.COMPLETED,
            output={"ok": True},
            start_time=datetime.now(),
            end_time=datetime.now(),
        )


def _install_common_stubs(engine, models, retry_engine):
    engine.retry_engine = retry_engine
    engine.budget_manager = types.SimpleNamespace(
        create_step_budget=lambda step_id: object(),
    )
    engine.circuit_breaker_manager = types.SimpleNamespace(
        is_agent_available=lambda agent_type: True,
    )
    engine.loop_detector = types.SimpleNamespace(
        is_step_in_loop=lambda wf_id, step_id: (False, None),
        record_step_execution=lambda wf_id, step_id, data: False,
    )


def _run_dispatch(engine, step, workflow_def, models):
    ctx = models.WorkflowContext(workflow_id="wf-retry", session_id="sess-retry")
    ctx._workflow_definition = workflow_def
    return asyncio.run(engine._execute_enhanced_step(step, ctx, {}))


def _tool_step(models, **overrides):
    params = dict(id="video_generator", task="gen", step_type="tool", tool_name="x_tool")
    params.update(overrides)
    return models.WorkflowStep(**params)


# ---------------------------------------------------------------------------
# 1. global_retry_policy пробрасывается в retry-движок
# ---------------------------------------------------------------------------
def test_enhanced_dispatch_uses_global_retry_policy():
    engine = _enhanced_engine_instance()
    models = _models()
    retry_engine = _CapturingRetryEngine(models)
    _install_common_stubs(engine, models, retry_engine)

    policy = models.RetryPolicy(
        max_retries=2,
        base_delay=1.0,
        max_delay=300.0,
        retry_on_errors=["network_error", "rate_limit", "timeout"],
    )
    wf = models.WorkflowDefinition(
        name="wf",
        steps=[],
        global_retry_policy=policy,
        error_handling={"auto_retry_transient": True},
    )
    step = _tool_step(models, timeout=17)

    result = _run_dispatch(engine, step, wf, models)

    assert result.status == models.StepStatus.COMPLETED
    cap = retry_engine.captured
    assert cap["max_retries"] == 2
    assert cap["base_delay"] == 1.0
    assert cap["max_delay"] == 300.0
    assert cap["retry_on_errors"] == ["network_error", "rate_limit", "timeout"]
    assert cap["attempt_timeout"] == 17
    assert cap["deadline"] is None
    # backoff_multiplier остаётся прежним хардкодом enhanced (1.5), не из политики
    assert cap["backoff_multiplier"] == 1.5


# ---------------------------------------------------------------------------
# 2. step.retry_policy имеет приоритет над global
# ---------------------------------------------------------------------------
def test_enhanced_dispatch_step_policy_overrides_global():
    engine = _enhanced_engine_instance()
    models = _models()
    retry_engine = _CapturingRetryEngine(models)
    _install_common_stubs(engine, models, retry_engine)

    wf = models.WorkflowDefinition(
        name="wf",
        steps=[],
        global_retry_policy=models.RetryPolicy(max_retries=2),
        error_handling={},
    )
    step = _tool_step(
        models,
        retry_policy=models.RetryPolicy(
            max_retries=5, base_delay=0.5, max_delay=10.0, retry_on_errors=["rate_limit"],
        ),
    )

    _run_dispatch(engine, step, wf, models)
    cap = retry_engine.captured
    assert cap["max_retries"] == 5
    assert cap["base_delay"] == 0.5
    assert cap["retry_on_errors"] == ["rate_limit"]


# ---------------------------------------------------------------------------
# 3. auto_retry_transient=false → max_retries=0
# ---------------------------------------------------------------------------
def test_auto_retry_transient_false_forces_zero_retries():
    engine = _enhanced_engine_instance()
    models = _models()
    retry_engine = _CapturingRetryEngine(models)
    _install_common_stubs(engine, models, retry_engine)

    wf = models.WorkflowDefinition(
        name="wf",
        steps=[],
        global_retry_policy=models.RetryPolicy(max_retries=2),
        error_handling={"auto_retry_transient": False},
    )
    step = _tool_step(models)

    _run_dispatch(engine, step, wf, models)
    assert retry_engine.captured["max_retries"] == 0


# ---------------------------------------------------------------------------
# 4. metadata.retryable=false минует adaptive retry (single-attempt путь)
# ---------------------------------------------------------------------------
def test_retryable_false_bypasses_adaptive_retry():
    engine = _enhanced_engine_instance()
    models = _models()

    class _ExplodingRetryEngine:
        async def execute_with_retry(self, *args, **kwargs):
            raise AssertionError("adaptive retry не должен вызываться для retryable=false")

    _install_common_stubs(engine, models, _ExplodingRetryEngine())

    single_calls = {"n": 0}

    async def fake_single_attempt(retry_context):
        single_calls["n"] += 1
        step = retry_context["step"]
        return models.StepResult(
            step_id=step.id,
            status=models.StepStatus.COMPLETED,
            output={"ok": True},
            start_time=datetime.now(),
            end_time=datetime.now(),
        )

    engine._execute_single_step_attempt = fake_single_attempt

    wf = models.WorkflowDefinition(name="wf", steps=[], error_handling={})
    step = _tool_step(models, metadata={"retryable": False})

    result = _run_dispatch(engine, step, wf, models)
    assert result.status == models.StepStatus.COMPLETED
    assert single_calls["n"] == 1


# ---------------------------------------------------------------------------
# 5. AdaptiveRetryEngine.retry_on_errors: нетранзиент → одна попытка
# ---------------------------------------------------------------------------
def test_adaptive_retry_stops_on_non_retryable_class():
    _install_light_workflow_package()
    from workflow.resilience.retry import AdaptiveRetryEngine

    engine = AdaptiveRetryEngine()
    calls = {"n": 0}

    async def fail(context):
        calls["n"] += 1
        raise ValueError("invalid input: bad request 400")

    result = asyncio.run(
        engine.execute_with_retry(
            "step_x",
            fail,
            {},
            max_retries=3,
            base_delay=0,
            retry_on_errors=["network_error", "rate_limit", "timeout"],
        )
    )
    assert calls["n"] == 1, "нетранзиентная ошибка не должна повторяться"
    assert result.status.value == "failed"
    assert "Non-retryable" in (result.error or "")


# ---------------------------------------------------------------------------
# 6. AdaptiveRetryEngine.retry_on_errors: транзиент → повтор до исчерпания
# ---------------------------------------------------------------------------
def test_adaptive_retry_retries_transient_class():
    _install_light_workflow_package()
    from workflow.resilience.retry import AdaptiveRetryEngine

    engine = AdaptiveRetryEngine()
    calls = {"n": 0}

    async def fail(context):
        calls["n"] += 1
        raise ConnectionError("connection reset by peer")

    result = asyncio.run(
        engine.execute_with_retry(
            "step_y",
            fail,
            {},
            max_retries=2,
            base_delay=0,
            retry_on_errors=["network_error", "rate_limit", "timeout"],
        )
    )
    assert calls["n"] == 3, "транзиентная ошибка повторяется max_retries+1 раз"
    assert result.status.value == "failed"

# ---------------------------------------------------------------------------
# 7. module-level classify_error: тексты таймаутов классифицируются как retryable
# ---------------------------------------------------------------------------
def test_classify_error_recognizes_timeout_texts():
    _install_light_workflow_package()
    from workflow.retry_engine import classify_error

    assert classify_error(RuntimeError("Step timed out after 900 seconds")) == "network_error"
    assert classify_error(RuntimeError("Превышено время ожидания AITUNNEL task")) == "network_error"
    assert classify_error(RuntimeError("provider task timeout")) == "network_error"
    # нетранзиентные классы не задеты
    assert classify_error(ValueError("bad request 400 invalid")) == "validation_error"
    assert classify_error(RuntimeError("401 unauthorized")) == "auth_error"


# ---------------------------------------------------------------------------
# 8. JudgeRetryRequested НЕ фильтруется retry_on_errors — вердикт судьи ретраится.
#    Регресс H-2: непустой дефолтный retry_on_errors классифицировал вердикт как
#    unknown_error и валил агентный шаг без единой повторной попытки.
# ---------------------------------------------------------------------------
def test_judge_retry_bypasses_retry_on_errors_filter():
    _install_light_workflow_package()
    from workflow.resilience.retry import AdaptiveRetryEngine, JudgeRetryRequested

    engine = AdaptiveRetryEngine()
    calls = {"n": 0}

    async def judge_reject(context):
        calls["n"] += 1
        # classify_error("Decision: ...") == "unknown_error" (нет в retry_on_errors);
        # без bypass это дало бы ровно 1 попытку.
        raise JudgeRetryRequested("Decision: retry - Quality poor")

    result = asyncio.run(
        engine.execute_with_retry(
            "agent_step",
            judge_reject,
            {},
            max_retries=2,
            base_delay=0,
            retry_on_errors=["network_error", "rate_limit", "timeout"],
        )
    )
    assert calls["n"] == 3, "вердикт судьи должен ретраиться max_retries+1 раз, а не отсекаться классовым фильтром"
    assert result.status.value == "failed"
    # Не прошёл через классовый фильтр → не помечен Non-retryable
    assert "Non-retryable" not in (result.error or "")


# ---------------------------------------------------------------------------
# 9. FAILED-РЕЗУЛЬТАТ (возвращён, не брошен) с нетранзиентным классом отсекается.
#    _execute_step_with_policy ловит исключение шага и ВОЗВРАЩАЕТ FAILED, минуя
#    except-ветку фильтра — без ветки для FAILED-результата M-1 не срабатывал.
# ---------------------------------------------------------------------------
def test_adaptive_retry_stops_on_non_retryable_failed_result():
    _install_light_workflow_package()
    import workflow.resilience.retry as retry_mod
    from workflow.resilience.retry import AdaptiveRetryEngine
    # StepResult/StepStatus берём из пространства имён retry-модуля: именно этот класс
    # использует его isinstance()/StepStatus.FAILED (модуль закэширован в sys.modules и не
    # перезагружается фикстурой — иначе ловушка идентичности классов даст ложный «успех»).
    StepResult, StepStatus = retry_mod.StepResult, retry_mod.StepStatus

    engine = AdaptiveRetryEngine()
    calls = {"n": 0}

    async def return_failed(context):
        calls["n"] += 1
        # classify_error("401 unauthorized") == "auth_error" (нет в retry_on_errors)
        return StepResult(
            step_id="step_z",
            status=StepStatus.FAILED,
            error="401 unauthorized",
            start_time=datetime.now(),
            end_time=datetime.now(),
        )

    result = asyncio.run(
        engine.execute_with_retry(
            "step_z",
            return_failed,
            {},
            max_retries=3,
            base_delay=0,
            retry_on_errors=["network_error", "rate_limit", "timeout"],
        )
    )
    assert calls["n"] == 1, "нетранзиентный FAILED-результат не должен ретраиться вслепую"
    assert result.status.value == "failed"
    assert "Non-retryable" in (result.error or "")


# ---------------------------------------------------------------------------
# 10. FAILED-результат с ТРАНЗИЕНТНЫМ классом по-прежнему ретраится до исчерпания.
# ---------------------------------------------------------------------------
def test_adaptive_retry_retries_transient_failed_result():
    _install_light_workflow_package()
    import workflow.resilience.retry as retry_mod
    from workflow.resilience.retry import AdaptiveRetryEngine
    StepResult, StepStatus = retry_mod.StepResult, retry_mod.StepStatus

    engine = AdaptiveRetryEngine()
    calls = {"n": 0}

    async def return_failed(context):
        calls["n"] += 1
        # classify_error("connection reset") == "network_error" (в retry_on_errors)
        return StepResult(
            step_id="step_w",
            status=StepStatus.FAILED,
            error="connection reset by peer",
            start_time=datetime.now(),
            end_time=datetime.now(),
        )

    result = asyncio.run(
        engine.execute_with_retry(
            "step_w",
            return_failed,
            {},
            max_retries=2,
            base_delay=0,
            retry_on_errors=["network_error", "rate_limit", "timeout"],
        )
    )
    assert calls["n"] == 3, "транзиентный FAILED-результат повторяется max_retries+1 раз"
    assert result.status.value == "failed"
