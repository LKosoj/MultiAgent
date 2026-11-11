"""Общие подделки для тестов управления прогоном: движок без сети и платных вызовов."""

import asyncio
import json
import sqlite3
from types import SimpleNamespace

from workflow.models import StepResult, StepStatus, WorkflowResult, WorkflowStatus


class FakeStore:
    def __init__(self, db_path):
        self.db_path = str(db_path)
        self.checkpoints = {}
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS workflow_checkpoints (
                workflow_id TEXT NOT NULL, timestamp TEXT NOT NULL, status TEXT NOT NULL, current_step TEXT,
                completed_steps TEXT, failed_steps TEXT, context TEXT, step_results TEXT,
                resumable BOOLEAN DEFAULT TRUE, metadata TEXT, PRIMARY KEY (workflow_id, timestamp))""")

    async def get_latest_checkpoint(self, workflow_id):
        return self.checkpoints.get(workflow_id)

    def add(self, workflow_id, status, completed=(), variables=None, outputs=None, ts="2026-11-01T10:00:00",
            current_step=None):
        context = SimpleNamespace(workflow_id=workflow_id, session_id="p", variables=dict(variables or {}),
                                  step_outputs=dict(outputs or {}), metadata={})
        self.checkpoints[workflow_id] = SimpleNamespace(
            context=context, step_results={}, current_step=current_step, completed_steps=list(completed),
            status=status)
        ctx_json = json.dumps({"session_id": "p", "variables": dict(variables or {})})
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("INSERT OR REPLACE INTO workflow_checkpoints VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (workflow_id, ts, status, current_step, json.dumps(list(completed)), "[]", ctx_json,
                          "{}", 1, "{}"))


class FakeStateManager:
    def __init__(self, store):
        self.store = store
        self.saved = []

    async def save_checkpoint(self, **kwargs):
        self.saved.append(kwargs)


class FakeEngine:
    """Повторяет порядок вызовов настоящего движка: проверка отмены, старт шага, завершение шага."""

    def __init__(self, tmp_path, step_delay=0.0, fail_steps=(), on_step=None):
        self.state_manager = FakeStateManager(FakeStore(tmp_path / "wf.db"))
        self.step_delay = step_delay
        self.fail_steps = set(fail_steps)
        self.on_step = on_step
        self.cancelled = set()
        self.cancel_calls = []
        self.executed = []
        self.contexts = []
        self.definitions = []

    async def cancel_workflow(self, workflow_id, reason="User cancelled"):
        self.cancelled.add(workflow_id)
        self.cancel_calls.append((workflow_id, reason))

    async def _execute_enhanced_step(self, step, context, previous_results):
        self.executed.append(step.id)
        if self.on_step:
            self.on_step(step, context)
        await asyncio.sleep(self.step_delay)
        status = StepStatus.FAILED if step.id in self.fail_steps else StepStatus.COMPLETED
        return StepResult(step_id=step.id, status=status, duration_seconds=0.5,
                          error="сломан" if status == StepStatus.FAILED else None)

    async def _on_step_completed(self, workflow_id, step, step_result, context, step_results):
        step_results[step.id] = step_result

    async def execute_workflow(self, definition, context):
        self.definitions.append(definition)
        self.contexts.append(context)
        results = {}
        status = WorkflowStatus.COMPLETED
        for step in definition.steps:
            if context.workflow_id in self.cancelled:
                status = WorkflowStatus.CANCELLED
                break
            result = await self._execute_enhanced_step(step, context, dict(results))
            if result.status == StepStatus.FAILED:
                results[step.id] = result
                status = WorkflowStatus.FAILED
                break
            await self._on_step_completed(context.workflow_id, step, result, context, results)
        from datetime import datetime
        return WorkflowResult(workflow_id=context.workflow_id, status=status, start_time=datetime.now(),
                              step_results=results, error="сбой" if status == WorkflowStatus.FAILED else None)


def make_plan(project_id, steps, mode="from_step", **extra):
    plan = {"version": 1, "mode": mode, "project_id": project_id, "series_id": "main", "pipeline_hash": "", "steps_to_run": list(steps),
            "skip_steps": [], "gated_off": [], "start_step": steps[0] if steps else None,
            "source_workflow_id": None, "workflow_id": f"sbm_partial_{project_id}_abcdef12", "inputs": {},
            "input_sources": {}, "step_params": {}, "scope": {"shot_keys": [], "kinds": []},
            "prepare": {"hook": None}, "env": {}, "problems": [], "can_start": True, "plan_digest": "x",
            "estimate": None}
    plan.update(extra)
    return plan


def make_series_project(root, project_id="demo"):
    """Создать проект и пустую серию для проверок Studio."""
    project = root / project_id
    project.mkdir(parents=True, exist_ok=True)
    (project / "project.json").write_text(json.dumps({"format": "storybook-series-v1", "project_id": project_id, "title": project_id, "resources": {}}), encoding="utf-8")
    (project / "shared").mkdir(exist_ok=True)
    series = project / "series" / "main"
    series.mkdir(parents=True, exist_ok=True)
    (series / "series.json").write_text(json.dumps({"series_id": "main", "title": "Main", "snapshot": {}}), encoding="utf-8")
    return series
