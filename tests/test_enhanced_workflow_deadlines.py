from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import asdict
import importlib.util
from pathlib import Path
import sys
import types

import pytest

from workflow.deadline import (
    DeadlineBudget,
    WorkflowDeadlineExceeded,
    execute_step_attempt,
)
from workflow.models import (
    ResourceLimits,
    RetryPolicy,
    StepResult,
    StepStatus,
    WorkflowContext,
    WorkflowStep,
)
from workflow.orchestration.parallel_executor import ParallelWorkflowExecutor
from workflow.resilience.retry import AdaptiveRetryEngine
from workflow.retry_engine import RetryEngine


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


def test_remaining_time_is_monotonic_for_one_persisted_deadline() -> None:
    clock = _Clock(monotonic=5.0, wall=1_000.0)
    budget = DeadlineBudget.from_deadline_at_ms(
        1_010_000,
        monotonic=clock.monotonic,
        wall_time=clock.wall,
    )

    assert budget.remaining_seconds() == 10.0
    clock.monotonic_value += 3.0
    assert budget.remaining_seconds() == 7.0


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


def test_base_retry_does_not_sleep_past_the_shared_deadline(monkeypatch) -> None:
    import workflow.retry_engine as retry_module

    clock = _Clock()
    budget = DeadlineBudget.from_duration(
        1.0,
        monotonic=clock.monotonic,
        wall_time=clock.wall,
    )
    retry_engine = RetryEngine()
    calls = 0
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    async def fail(_context: object) -> None:
        nonlocal calls
        calls += 1
        raise ConnectionError("connection reset")

    monkeypatch.setattr(retry_module.asyncio, "sleep", fake_sleep)

    with pytest.raises(WorkflowDeadlineExceeded, match="retry backoff"):
        asyncio.run(
            retry_engine.execute_with_retry(
                "base-step",
                fail,
                {},
                retry_policy=RetryPolicy(max_retries=1, base_delay=2.0),
                deadline=budget,
            )
        )

    assert calls == 1
    assert sleeps == []


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


def test_parallel_siblings_receive_the_same_absolute_deadline() -> None:
    seen_deadlines: list[DeadlineBudget] = []

    async def execute(step: WorkflowStep, context: WorkflowContext) -> StepResult:
        seen_deadlines.append(context._deadline_budget)
        return StepResult(step_id=step.id, status=StepStatus.COMPLETED)

    async def run() -> None:
        executor = ParallelWorkflowExecutor(max_concurrent=2)
        context = WorkflowContext(workflow_id="wf-shared-deadline")
        deadline = DeadlineBudget.from_duration(5.0)
        context._deadline_budget = deadline
        result = await executor.execute_steps_parallel(
            [
                WorkflowStep(id="left", task="left"),
                WorkflowStep(id="right", task="right"),
            ],
            context,
            step_executor=execute,
            dependency_checker=lambda _step, _results: True,
            condition_checker=lambda _step, _context: False,
        )

        assert set(result) == {"left", "right"}
        assert seen_deadlines == [deadline, deadline]

    asyncio.run(run())


@pytest.mark.parametrize("step_timeout", [10, None])
def test_non_retryable_step_uses_global_deadline(
    monkeypatch,
    step_timeout: int | None,
) -> None:
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
            timeout=step_timeout,
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
        engine._install_text_to_sql_typed_runtime = lambda _context: None
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
        assert (
            result.terminal_outcome.status is models.TextToSqlTerminalStatus.TIMED_OUT
        )
        assert result.terminal_outcome.reason_code == "TIMED_OUT"
        assert result.terminal_outcome.executed is False
        assert result.terminal_outcome.audited is False
        assert dict(result.terminal_outcome.execution) == {}
        assert dict(result.terminal_outcome.audit) == {}
        assert dict(result.terminal_outcome.result_review) == {}
        assert result.final_output["final"] == result.terminal_outcome.to_mapping()


def test_text_to_sql_cancelled_outcome_has_no_ambiguity() -> None:
    with _light_enhanced_engine() as (engine, _enhanced_module):
        models = sys.modules["workflow.models"]
        context = models.WorkflowContext(
            workflow_id="wf-cancel",
            variables={"run_id": "run-cancel"},
        )

        terminal = engine._text_to_sql_cancelled_outcome(context, "wf-cancel")

        assert terminal.status is models.TextToSqlTerminalStatus.CANCELLED
        assert terminal.executed is False
        assert terminal.ambiguity is None
        assert dict(terminal.result_review) == {}


def test_execute_step_attempt_distinguishes_step_raised_timeout_from_wait_for_cutoff() -> (
    None
):
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


def test_enhanced_engine_does_not_create_a_second_deadline_from_yaml_limits() -> None:
    with _light_enhanced_engine() as (engine, _enhanced_module):
        models = sys.modules["workflow.models"]
        context = models.WorkflowContext(workflow_id="wf-persisted-only")
        workflow = models.WorkflowDefinition(
            name="generic",
            global_resource_limits=ResourceLimits(max_duration_seconds=30),
        )

        assert engine._ensure_workflow_deadline(workflow, context) is None
        assert not hasattr(context, "_deadline_budget")


def test_enhanced_step_budget_uses_configured_step_duration_limit() -> None:
    with _light_enhanced_engine() as (engine, _enhanced_module):
        models = sys.modules["workflow.models"]
        observed: dict[str, object] = {}

        class BudgetManager:
            def create_step_budget(self, step_id, limits=None):
                observed["step_id"] = step_id
                observed["limits"] = limits
                return object()

        class CircuitBreakerManager:
            def is_agent_available(self, _agent_name):
                return True

        class LoopDetector:
            def is_step_in_loop(self, _workflow_id, _step_id):
                return False, None

            def record_step_execution(self, _workflow_id, _step_id, _execution_data):
                return False

        class RetryEngine:
            async def execute_with_retry(self, **_kwargs):
                return models.StepResult(
                    step_id="schema_research",
                    status=models.StepStatus.COMPLETED,
                )

        engine.budget_manager = BudgetManager()
        engine.circuit_breaker_manager = CircuitBreakerManager()
        engine.loop_detector = LoopDetector()
        engine.retry_engine = RetryEngine()

        step = models.WorkflowStep(
            id="schema_research",
            task="research",
            resource_limits=ResourceLimits(max_duration_seconds=14_400),
        )
        result = asyncio.run(
            engine._execute_enhanced_step(
                step,
                models.WorkflowContext(workflow_id="wf-step-budget"),
                {},
            )
        )

        assert result.status is models.StepStatus.COMPLETED
        assert observed["step_id"] == "schema_research"
        assert {
            budget_type.value: limit
            for budget_type, limit in observed["limits"].items()
        } == {"time": 14_400}


def test_base_finalization_is_bounded_by_the_shared_deadline() -> None:
    async def run() -> None:
        with _light_enhanced_engine() as (engine, _enhanced_module):
            models = sys.modules["workflow.models"]
            context = models.WorkflowContext(workflow_id="wf-finalization-timeout")
            context._deadline_budget = DeadlineBudget.from_duration(0.01)
            engine.aggregator = types.SimpleNamespace(
                aggregate_final_result=lambda *_args: asyncio.sleep(0.1),
            )

            async def not_cancelled(_workflow_id: str) -> bool:
                return False

            async def mark_completed(_workflow_id: str, _final_output: object) -> None:
                return None

            engine._is_workflow_cancelled = not_cancelled
            engine._on_workflow_completed = mark_completed

            with pytest.raises(WorkflowDeadlineExceeded, match="result aggregation"):
                await engine._finalize_workflow_execution(
                    models.WorkflowDefinition(name="generic"),
                    context,
                    {},
                    models.datetime.now(),
                    None,
                )

    asyncio.run(run())


def test_legacy_retry_fallback_keeps_the_deadline_cap() -> None:
    async def run() -> None:
        with _light_enhanced_engine() as (engine, _enhanced_module):
            models = sys.modules["workflow.models"]
            context = models.WorkflowContext(workflow_id="wf-legacy-retry")
            context._deadline_budget = DeadlineBudget.from_duration(0.01)
            calls: list[bool] = []

            class LegacyRetryEngine:
                async def execute_with_retry(
                    self,
                    *,
                    step_id,
                    step_func,
                    context,
                    retry_policy,
                ):
                    calls.append(True)
                    await asyncio.sleep(0.1)
                    return models.StepResult(
                        step_id="legacy-step",
                        status=models.StepStatus.COMPLETED,
                    )

            engine.retry_engine = LegacyRetryEngine()
            step = models.WorkflowStep(id="legacy-step", task="legacy")
            workflow = models.WorkflowDefinition(name="generic")

            with pytest.raises(WorkflowDeadlineExceeded):
                await asyncio.wait_for(
                    engine._execute_workflow_step(step, context, workflow),
                    timeout=0.05,
                )

            assert calls == [True]

    asyncio.run(run())


def test_retry_engine_inner_type_error_is_not_replayed() -> None:
    async def run() -> None:
        with _light_enhanced_engine() as (engine, _enhanced_module):
            models = sys.modules["workflow.models"]
            context = models.WorkflowContext(workflow_id="wf-inner-type-error")
            context._deadline_budget = DeadlineBudget.from_duration(1.0)
            calls = 0

            class RetryEngineWithInnerError:
                async def execute_with_retry(self, **_kwargs):
                    nonlocal calls
                    calls += 1
                    raise TypeError("unexpected keyword argument 'deadline'")

            engine.retry_engine = RetryEngineWithInnerError()
            step = models.WorkflowStep(id="side-effect-step", task="one call")

            with pytest.raises(TypeError, match="unexpected keyword argument"):
                await engine._execute_workflow_step(
                    step,
                    context,
                    models.WorkflowDefinition(name="generic"),
                )

            assert calls == 1

    asyncio.run(run())


def test_checkpoint_and_final_state_writes_are_bounded_by_deadline() -> None:
    async def run() -> None:
        with _light_enhanced_engine() as (engine, _enhanced_module):
            models = sys.modules["workflow.models"]
            never = asyncio.Event()
            context = models.WorkflowContext(workflow_id="wf-state-write")
            context._deadline_budget = DeadlineBudget.from_duration(0.01)
            engine.state_manager = types.SimpleNamespace(
                save_checkpoint=lambda **_kwargs: never.wait(),
                mark_workflow_completed=lambda *_args: never.wait(),
            )
            step = models.WorkflowStep(id="checkpoint-step", task="checkpoint")
            result = models.StepResult(
                step_id=step.id,
                status=models.StepStatus.COMPLETED,
            )

            with pytest.raises(WorkflowDeadlineExceeded, match="checkpoint"):
                await asyncio.wait_for(
                    engine._on_step_completed(
                        context.workflow_id,
                        step,
                        result,
                        context,
                        {step.id: result},
                    ),
                    timeout=0.05,
                )

            context._deadline_budget = DeadlineBudget.from_duration(0.01)
            with pytest.raises(WorkflowDeadlineExceeded, match="completion checkpoint"):
                await asyncio.wait_for(
                    engine._on_workflow_completed(
                        context.workflow_id,
                        {},
                        context=context,
                    ),
                    timeout=0.05,
                )

    asyncio.run(run())


def test_workflow_definition_write_is_bounded_by_deadline() -> None:
    async def run() -> None:
        with _light_enhanced_engine() as (engine, _enhanced_module):
            models = sys.modules["workflow.models"]
            never = asyncio.Event()
            context = models.WorkflowContext(workflow_id="wf-definition-write")
            context._deadline_budget = DeadlineBudget.from_duration(0.01)
            engine.state_manager = types.SimpleNamespace(
                save_workflow_definition=lambda *_args: never.wait(),
            )

            with pytest.raises(WorkflowDeadlineExceeded, match="definition checkpoint"):
                await asyncio.wait_for(
                    engine._save_workflow_definition(
                        context,
                        models.WorkflowDefinition(name="generic"),
                    ),
                    timeout=0.05,
                )

    asyncio.run(run())


def test_expiry_after_terminal_outcome_preserves_primary_text_to_sql_result() -> None:
    async def run() -> None:
        with _light_enhanced_engine() as (engine, _enhanced_module):
            engine._install_text_to_sql_typed_runtime = lambda _context: None
            models = sys.modules["workflow.models"]
            context = models.WorkflowContext(
                workflow_id="wf-terminal-before-expiry",
                variables={"run_id": "run-terminal-before-expiry"},
            )
            context._deadline_budget = DeadlineBudget.from_duration(0.02)
            workflow = models.WorkflowDefinition(
                name="text_to_sql_pipeline",
                metadata={"category": "text_to_sql"},
            )
            primary = engine._text_to_sql_failure_outcome(
                context,
                {},
                "DB_AUDIT_FAILED",
                "authoritative db audit result",
            )
            engine.metrics_collector = types.SimpleNamespace(
                record_workflow_start=lambda *_args: None,
            )
            engine.budget_manager = types.SimpleNamespace(
                create_workflow_budget=lambda *_args: object(),
            )
            engine._db_audit_has_terminal_step_result = lambda _results: True
            engine._derive_text_to_sql_terminal_outcome = lambda *_args: primary

            async def started(*_args, **_kwargs):
                return None

            async def steps(*_args, **_kwargs):
                return {}

            async def not_cancelled(_workflow_id):
                return False

            async def slow_aggregation(*_args):
                await asyncio.sleep(0.1)

            async def failed(*_args, **_kwargs):
                return None

            async def released(*_args, **_kwargs):
                return None

            engine._on_workflow_started = started
            engine._execute_enhanced_steps = steps
            engine._is_workflow_cancelled = not_cancelled
            engine.aggregator = types.SimpleNamespace(
                aggregate_final_result=slow_aggregation,
            )
            engine._on_workflow_failed = failed
            engine._release_workflow_resources = released

            result = await engine._execute_enhanced_workflow(workflow, context)

            assert result.terminal_outcome == primary
            assert result.terminal_outcome.reason_code == "DB_AUDIT_FAILED"
            assert (
                result.terminal_outcome.status
                is not models.TextToSqlTerminalStatus.TIMED_OUT
            )

    asyncio.run(run())


def test_terminal_evidence_is_derived_before_terminalization_deadline_gate() -> None:
    async def run() -> None:
        with _light_enhanced_engine() as (engine, _enhanced_module):
            engine._install_text_to_sql_typed_runtime = lambda _context: None
            models = sys.modules["workflow.models"]
            clock = _Clock()
            context = models.WorkflowContext(
                workflow_id="wf-terminal-boundary",
                variables={"run_id": "run-terminal-boundary"},
            )
            context._deadline_budget = DeadlineBudget(
                deadline_monotonic=1.0,
                deadline_at_ms=1_001_000,
                monotonic=clock.monotonic,
                wall_time=clock.wall,
            )
            workflow = models.WorkflowDefinition(
                name="text_to_sql_pipeline",
                metadata={"category": "text_to_sql"},
            )
            primary = engine._text_to_sql_failure_outcome(
                context,
                {},
                "DB_AUDIT_FAILED",
                "authoritative result at deadline boundary",
            )
            derived = False
            engine.metrics_collector = types.SimpleNamespace(
                record_workflow_start=lambda *_args: None,
            )
            engine.budget_manager = types.SimpleNamespace(
                create_workflow_budget=lambda *_args: object(),
            )
            engine._db_audit_has_terminal_step_result = lambda _results: True

            def derive(*_args):
                nonlocal derived
                derived = True
                return primary

            async def started(*_args, **_kwargs):
                return None

            async def steps(*_args, **_kwargs):
                clock.monotonic_value = 1.0
                return {}

            async def released(*_args, **_kwargs):
                return None

            async def failed(*_args, **_kwargs):
                return None

            engine._derive_text_to_sql_terminal_outcome = derive
            engine._on_workflow_started = started
            engine._execute_enhanced_steps = steps
            engine._on_workflow_failed = failed
            engine._release_workflow_resources = released

            result = await engine._execute_enhanced_workflow(workflow, context)

            assert derived
            assert result.terminal_outcome == primary

    asyncio.run(run())


def test_direct_text_to_sql_without_persisted_deadline_fails_before_work() -> None:
    with _light_enhanced_engine() as (engine, _enhanced_module):
        models = sys.modules["workflow.models"]
        workflow = models.WorkflowDefinition(
            name="text_to_sql_pipeline",
            metadata={"category": "text_to_sql"},
        )

        with pytest.raises(models.WorkflowExecutionError, match="persisted deadline"):
            asyncio.run(engine._execute_enhanced_workflow(workflow))
