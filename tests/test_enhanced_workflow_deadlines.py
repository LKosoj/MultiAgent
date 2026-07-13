from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import asdict
import importlib.util
from pathlib import Path
import sys
import types

import pytest

from workflow.deadline import DeadlineBudget, WorkflowDeadlineExceeded, execute_step_attempt
from workflow.models import StepResult, StepStatus, WorkflowContext, WorkflowStep
from workflow.orchestration.parallel_executor import ParallelWorkflowExecutor
from workflow.resilience.retry import AdaptiveRetryEngine


ROOT = Path(__file__).resolve().parents[1]
_LIGHT_MODULES = (
    "workflow",
    "workflow.engine",
    "workflow.enhanced_engine",
    "workflow.models",
    "workflow.state_manager",
    "workflow.retry_engine",
    "workflow.resource_manager",
    "agent_system",
)


def _load_module(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {module_name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@contextmanager
def _light_enhanced_engine():
    missing = object()
    saved = {name: sys.modules.get(name, missing) for name in _LIGHT_MODULES}
    try:
        for name in _LIGHT_MODULES:
            sys.modules.pop(name, None)

        workflow_package = types.ModuleType("workflow")
        workflow_package.__path__ = [str(ROOT / "workflow")]
        sys.modules["workflow"] = workflow_package

        agent_system = types.ModuleType("agent_system")
        agent_system.DynamicAgentSystem = type("DynamicAgentSystem", (), {})
        sys.modules["agent_system"] = agent_system

        for module_name in (
            "workflow.models",
            "workflow.state_manager",
            "workflow.retry_engine",
            "workflow.resource_manager",
        ):
            relative = module_name.split(".", 1)[1].replace(".", "/") + ".py"
            module = _load_module(module_name, ROOT / "workflow" / relative)
            setattr(workflow_package, module_name.rsplit(".", 1)[1], module)

        engine_module = _load_module(
            "workflow.engine",
            ROOT / "workflow" / "engine.py",
        )
        workflow_package.engine = engine_module
        enhanced_module = _load_module(
            "workflow.enhanced_engine",
            ROOT / "workflow" / "enhanced_engine.py",
        )
        yield object.__new__(enhanced_module.EnhancedWorkflowEngine), enhanced_module
    finally:
        for name, module in saved.items():
            if module is missing:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


class _Clock:
    def __init__(self, *, monotonic: float = 0.0, wall: float = 1_000.0) -> None:
        self.monotonic_value = monotonic
        self.wall_value = wall
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.monotonic_value

    def wall(self) -> float:
        return self.wall_value

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.monotonic_value += delay


def test_queue_wait_is_consumed_when_rehydrating_wall_deadline() -> None:
    clock = _Clock(monotonic=25.0, wall=1_000.0)

    budget = DeadlineBudget.from_deadline_at_ms(
        1_003_000,
        monotonic=clock.monotonic,
        wall_time=clock.wall,
    )

    assert budget.deadline_monotonic == 28.0
    assert budget.remaining_seconds() == 3.0
    assert budget.deadline_at_ms == 1_003_000


def test_private_context_deadline_is_not_a_dataclass_field() -> None:
    clock = _Clock()
    context = WorkflowContext(workflow_id="wf-deadline")
    context._deadline_budget = DeadlineBudget.from_duration(
        5.0,
        monotonic=clock.monotonic,
        wall_time=clock.wall,
    )

    assert "_deadline_budget" not in asdict(context)


def test_backoff_fails_before_sleep_that_would_exhaust_global_deadline() -> None:
    clock = _Clock()
    budget = DeadlineBudget.from_duration(
        1.0,
        monotonic=clock.monotonic,
        wall_time=clock.wall,
    )
    engine = AdaptiveRetryEngine(sleep=clock.sleep, jitter=lambda _a, _b: 0.0)
    calls = 0

    async def fail(_context: object) -> StepResult:
        nonlocal calls
        calls += 1
        raise ConnectionError("connection reset")

    with pytest.raises(WorkflowDeadlineExceeded, match="retry backoff"):
        asyncio.run(
            engine.execute_with_retry(
                "step",
                fail,
                {},
                max_retries=2,
                base_delay=2.0,
                retry_on_errors=["network_error"],
                deadline=budget,
            )
        )

    assert calls == 1
    assert clock.sleeps == []


def test_retry_attempt_timeout_is_capped_by_remaining_budget(monkeypatch) -> None:
    import workflow.deadline as deadline_module

    clock = _Clock()
    budget = DeadlineBudget.from_duration(
        6.0,
        monotonic=clock.monotonic,
        wall_time=clock.wall,
    )
    engine = AdaptiveRetryEngine(sleep=clock.sleep, jitter=lambda _a, _b: 0.0)
    timeouts: list[float] = []
    calls = 0

    async def deterministic_wait_for(awaitable, timeout: float):
        timeouts.append(timeout)
        return await awaitable

    monkeypatch.setattr(deadline_module.asyncio, "wait_for", deterministic_wait_for)

    async def attempt(_context: object) -> StepResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            clock.monotonic_value += 3.0
            raise ConnectionError("connection reset")
        return StepResult(
            step_id="step",
            status=StepStatus.COMPLETED,
            quality_score=1.0,
        )

    result = asyncio.run(
        engine.execute_with_retry(
            "step",
            attempt,
            {},
            max_retries=1,
            base_delay=1.0,
            retry_on_errors=["network_error"],
            attempt_timeout=5.0,
            deadline=budget,
        )
    )

    assert result.status is StepStatus.COMPLETED
    assert timeouts == [5.0, 2.0]
    assert clock.sleeps == [1.0]


def test_step_attempt_timeout_remains_retryable_with_budget_left(monkeypatch) -> None:
    import workflow.deadline as deadline_module

    clock = _Clock()
    budget = DeadlineBudget.from_duration(
        10.0,
        monotonic=clock.monotonic,
        wall_time=clock.wall,
    )
    engine = AdaptiveRetryEngine(sleep=clock.sleep, jitter=lambda _a, _b: 0.0)
    timeouts: list[float] = []

    async def deterministic_wait_for(awaitable, timeout: float):
        timeouts.append(timeout)
        if len(timeouts) == 1:
            awaitable.close()
            raise asyncio.TimeoutError
        return await awaitable

    monkeypatch.setattr(deadline_module.asyncio, "wait_for", deterministic_wait_for)

    async def succeed(_context: object) -> StepResult:
        return StepResult(
            step_id="step",
            status=StepStatus.COMPLETED,
            quality_score=1.0,
        )

    result = asyncio.run(
        engine.execute_with_retry(
            "step",
            succeed,
            {},
            max_retries=1,
            base_delay=0.0,
            retry_on_errors=["network_error"],
            attempt_timeout=1.0,
            deadline=budget,
        )
    )

    assert result.status is StepStatus.COMPLETED
    assert timeouts == [1.0, 1.0]


def test_step_error_at_deadline_boundary_yields_failed_result(monkeypatch) -> None:
    """T13b bug-fix regression.

    A step's own (non-timeout) error occurring exactly when the deadline is
    exhausted must surface as an honest FAILED StepResult carrying the
    original error — it must NOT be recolored into WorkflowDeadlineExceeded.
    Previously execute_with_retry's generic `except Exception` branch raised
    WorkflowDeadlineExceeded whenever `deadline.remaining_seconds() <= 0`,
    regardless of whether the exception had anything to do with the deadline
    (see removed lines in workflow/resilience/retry.py:execute_with_retry).
    """
    import workflow.deadline as deadline_module

    clock = _Clock()
    budget = DeadlineBudget.from_duration(
        2.0,
        monotonic=clock.monotonic,
        wall_time=clock.wall,
    )
    engine = AdaptiveRetryEngine(jitter=lambda _a, _b: 0.0)

    async def deterministic_wait_for(awaitable, timeout: float):
        return await awaitable

    monkeypatch.setattr(deadline_module.asyncio, "wait_for", deterministic_wait_for)

    async def fail_at_deadline(_context: object) -> StepResult:
        clock.monotonic_value += 2.0
        raise ConnectionError("connection reset")

    result = asyncio.run(
        engine.execute_with_retry(
            "step",
            fail_at_deadline,
            {},
            max_retries=0,
            attempt_timeout=5.0,
            deadline=budget,
        )
    )

    assert result.status is StepStatus.FAILED
    assert "connection reset" in (result.error or "")


def test_non_retryable_step_error_at_deadline_boundary_yields_failed_result() -> None:
    """T13b regression (enhanced_engine non-retryable path).

    _execute_non_retryable_attempt now delegates to the shared
    workflow.deadline.execute_step_attempt helper, so it must exhibit the same
    fixed semantics as execute_with_retry: a step's own (non-timeout) error at
    the deadline boundary surfaces as a FAILED StepResult from the outer
    _execute_enhanced_step handler, not a raised WorkflowDeadlineExceeded.
    """
    clock = _Clock()
    with _light_enhanced_engine() as (engine, _enhanced_module):
        models = sys.modules["workflow.models"]
        context = models.WorkflowContext(workflow_id="wf-non-retryable-error")
        context._deadline_budget = DeadlineBudget.from_duration(
            2.0,
            monotonic=clock.monotonic,
            wall_time=clock.wall,
        )
        workflow = models.WorkflowDefinition(name="generic")
        context._workflow_definition = workflow
        step = models.WorkflowStep(
            id="side-effect",
            task="run once",
            timeout=5,
            metadata={"retryable": False},
        )
        engine.budget_manager = types.SimpleNamespace(
            create_step_budget=lambda _step_id: object(),
        )
        engine.circuit_breaker_manager = types.SimpleNamespace(
            is_agent_available=lambda _agent_type: True,
        )
        engine.loop_detector = types.SimpleNamespace(
            is_step_in_loop=lambda _workflow_id, _step_id: (False, None),
            record_step_execution=lambda _workflow_id, _step_id, _data: False,
        )

        async def fail_at_deadline(_retry_context):
            clock.monotonic_value += 2.0
            raise ConnectionError("connection reset")

        engine._execute_single_step_attempt = fail_at_deadline
        result = asyncio.run(engine._execute_enhanced_step(step, context, {}))

        assert result.status is models.StepStatus.FAILED
        assert "connection reset" in (result.error or "")


def test_stuck_attempt_uses_one_real_wait_for_timeout() -> None:
    budget = DeadlineBudget.from_duration(0.02)
    engine = AdaptiveRetryEngine(jitter=lambda _a, _b: 0.0)
    never = asyncio.Event()

    async def stuck(_context: object) -> StepResult:
        await never.wait()
        raise AssertionError("unreachable")

    with pytest.raises(WorkflowDeadlineExceeded):
        asyncio.run(
            engine.execute_with_retry(
                "step",
                stuck,
                {},
                max_retries=0,
                attempt_timeout=10.0,
                deadline=budget,
            )
        )


def test_parallel_deadline_cancels_and_gathers_sibling() -> None:
    slow_started = asyncio.Event()
    slow_cancelled = asyncio.Event()

    async def execute(step: WorkflowStep, _context: WorkflowContext) -> StepResult:
        if step.id == "slow":
            slow_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                slow_cancelled.set()
        await slow_started.wait()
        raise WorkflowDeadlineExceeded("parallel deadline")

    async def run() -> None:
        executor = ParallelWorkflowExecutor(max_concurrent=2)
        steps = [
            WorkflowStep(id="slow", task="slow"),
            WorkflowStep(id="timeout", task="timeout"),
        ]
        with pytest.raises(WorkflowDeadlineExceeded, match="parallel deadline"):
            await executor.execute_steps_parallel(
                steps,
                WorkflowContext(workflow_id="wf-parallel-deadline"),
                step_executor=execute,
                dependency_checker=lambda _step, _results: True,
                condition_checker=lambda _step, _context: False,
            )
        assert slow_cancelled.is_set()
        assert executor.active_tasks == {}

    asyncio.run(run())


def test_non_retryable_step_timeout_is_capped_by_global_deadline(monkeypatch) -> None:
    import workflow.deadline as deadline_module

    clock = _Clock()
    with _light_enhanced_engine() as (engine, _enhanced_module):
        models = sys.modules["workflow.models"]
        context = models.WorkflowContext(workflow_id="wf-non-retryable")
        context._deadline_budget = DeadlineBudget.from_duration(
            3.0,
            monotonic=clock.monotonic,
            wall_time=clock.wall,
        )
        workflow = models.WorkflowDefinition(name="generic")
        context._workflow_definition = workflow
        step = models.WorkflowStep(
            id="side-effect",
            task="run once",
            timeout=10,
            metadata={"retryable": False},
        )
        timeouts: list[float] = []

        async def deterministic_wait_for(awaitable, timeout: float):
            timeouts.append(timeout)
            return await awaitable

        monkeypatch.setattr(
            deadline_module.asyncio,
            "wait_for",
            deterministic_wait_for,
        )
        engine.budget_manager = types.SimpleNamespace(
            create_step_budget=lambda _step_id: object(),
        )
        engine.circuit_breaker_manager = types.SimpleNamespace(
            is_agent_available=lambda _agent_type: True,
        )
        engine.loop_detector = types.SimpleNamespace(
            is_step_in_loop=lambda _workflow_id, _step_id: (False, None),
            record_step_execution=lambda _workflow_id, _step_id, _data: False,
        )

        async def execute_once(_retry_context):
            return models.StepResult(
                step_id=step.id,
                status=models.StepStatus.COMPLETED,
            )

        engine._execute_single_step_attempt = execute_once
        result = asyncio.run(engine._execute_enhanced_step(step, context, {}))

        assert result.status is models.StepStatus.COMPLETED
        assert timeouts == [3.0]


def test_expired_text_to_sql_budget_returns_authoritative_timed_out_result() -> None:
    clock = _Clock()
    expired = DeadlineBudget(
        deadline_monotonic=clock.monotonic(),
        deadline_at_ms=int(clock.wall() * 1000),
        monotonic=clock.monotonic,
        wall_time=clock.wall,
    )
    with _light_enhanced_engine() as (engine, _enhanced_module):
        models = sys.modules["workflow.models"]
        workflow = models.WorkflowDefinition(
            name="text_to_sql_pipeline",
            metadata={"category": "text_to_sql"},
        )
        context = models.WorkflowContext(
            workflow_id="wf-timeout",
            variables={"run_id": "run-timeout"},
        )
        context._deadline_budget = expired

        result = asyncio.run(engine._execute_enhanced_workflow(workflow, context))

        assert result.status is models.WorkflowStatus.FAILED
        assert result.terminal_outcome.status is models.TextToSqlTerminalStatus.TIMED_OUT
        assert result.terminal_outcome.reason_code == "TIMED_OUT"
        assert result.terminal_outcome.executed is False
        assert result.terminal_outcome.audited is False
        assert dict(result.terminal_outcome.execution) == {}
        assert dict(result.terminal_outcome.audit) == {}
        assert result.final_output["final"] == result.terminal_outcome.to_mapping()


def test_execute_step_attempt_distinguishes_step_raised_timeout_from_wait_for_cutoff() -> None:
    """T13b: workflow.deadline.execute_step_attempt must tell apart a step
    raising its own TimeoutError (business-logic timeout, message preserved)
    from asyncio.wait_for cutting a hung step off at the attempt_timeout
    boundary (generic "attempt timed out after N seconds" message)."""

    async def step_raises_timeout(_context: object) -> None:
        raise TimeoutError("business logic timed out")

    with pytest.raises(TimeoutError, match="business logic timed out"):
        asyncio.run(
            execute_step_attempt(
                "step",
                step_raises_timeout,
                {},
                attempt_timeout=5.0,
                deadline=None,
            )
        )

    async def step_hangs(_context: object) -> None:
        await asyncio.sleep(10)

    with pytest.raises(TimeoutError, match="attempt timed out after"):
        asyncio.run(
            execute_step_attempt(
                "step",
                step_hangs,
                {},
                attempt_timeout=0.01,
                deadline=None,
            )
        )


def test_expired_generic_workflow_preserves_exception_semantics() -> None:
    clock = _Clock()
    expired = DeadlineBudget(
        deadline_monotonic=clock.monotonic(),
        deadline_at_ms=int(clock.wall() * 1000),
        monotonic=clock.monotonic,
        wall_time=clock.wall,
    )
    with _light_enhanced_engine() as (engine, _enhanced_module):
        models = sys.modules["workflow.models"]
        context = models.WorkflowContext(workflow_id="wf-generic-timeout")
        context._deadline_budget = expired

        with pytest.raises(WorkflowDeadlineExceeded, match="workflow start"):
            asyncio.run(
                engine._execute_enhanced_workflow(
                    models.WorkflowDefinition(name="generic"),
                    context,
                )
            )
