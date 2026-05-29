from __future__ import annotations

import asyncio
from datetime import datetime
import unittest

from tests.workflow_test_utils import (
    load_light_parallel_executor,
    load_light_workflow_models,
)

workflow_models = load_light_workflow_models()
parallel_executor_module = load_light_parallel_executor()

StepResult = workflow_models.StepResult
StepStatus = workflow_models.StepStatus
WorkflowContext = workflow_models.WorkflowContext
WorkflowStep = workflow_models.WorkflowStep
ParallelWorkflowExecutor = parallel_executor_module.ParallelWorkflowExecutor


class TestParallelExecutorStop(unittest.IsolatedAsyncioTestCase):
    async def test_stop_checker_skips_remaining_steps_after_current_completion(self):
        step_one = WorkflowStep(id="step_one", task="first")
        step_two = WorkflowStep(id="step_two", task="second", depends_on=["step_one"])

        state = {"stop": False, "calls": 0}

        async def step_executor(step, context):
            state["calls"] += 1
            if step.id == "step_one":
                state["stop"] = True
            await asyncio.sleep(0)
            return StepResult(
                step_id=step.id,
                status=StepStatus.COMPLETED,
                start_time=datetime.now(),
                end_time=datetime.now(),
            )

        executor = ParallelWorkflowExecutor(max_concurrent=1)
        results = await executor.execute_steps_parallel(
            [step_one, step_two],
            WorkflowContext(workflow_id="wf_stop", session_id="wf_stop"),
            step_executor=step_executor,
            dependency_checker=lambda step, results: True,
            condition_checker=lambda step, context: False,
            stop_checker=lambda: state["stop"],
        )

        self.assertEqual(state["calls"], 1)
        self.assertEqual(results["step_one"].status, StepStatus.COMPLETED)
        self.assertEqual(results["step_two"].status, StepStatus.SKIPPED)
