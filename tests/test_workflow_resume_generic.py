"""
Тесты generic resume движка workflow.

Покрывают три части доработки:
  - Персистентность определения (save/get_workflow_definition) — round-trip
    через реальную временную SQLite-БД.
  - Пропуск завершённых шагов в исполнителях (skip_steps / initial_step_results)
    для последовательного и параллельного путей.
  - Оркестрацию WorkflowEngine.resume_workflow: корректные skip_steps/restored,
    фильтрация только COMPLETED-шагов и контрактные исключения.

Движок загружается «облегчённо» (agent_system застаблен), чтобы не тянуть
тяжёлый рантайм агентов; стаб agent_system восстанавливается после загрузки,
а engine кладётся в sys.modules под отдельным именем, чтобы не затенять реальный
workflow.engine для остальных тестов.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from tests.workflow_test_utils import (
    _load_module,
    load_light_parallel_executor,
    load_light_workflow_models,
)

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_models = load_light_workflow_models()
WorkflowDefinition = _models.WorkflowDefinition
WorkflowStep = _models.WorkflowStep
WorkflowContext = _models.WorkflowContext
WorkflowCheckpoint = _models.WorkflowCheckpoint
WorkflowStatus = _models.WorkflowStatus
StepResult = _models.StepResult
StepStatus = _models.StepStatus
WorkflowExecutionError = _models.WorkflowExecutionError
WorkflowNotFoundError = _models.WorkflowNotFoundError


def _load_light_engine():
    """Загружает workflow.engine с застабленным agent_system (без рантайма агентов)."""
    prev_agent_system = sys.modules.get("agent_system")
    stub = types.ModuleType("agent_system")
    stub.DynamicAgentSystem = type(
        "DynamicAgentSystem", (), {"__init__": lambda self, *a, **k: None}
    )
    sys.modules["agent_system"] = stub
    try:
        # Имя не 'workflow.engine', чтобы не затенять реальный модуль для прочих тестов.
        # __package__ всё равно 'workflow' → относительные импорты резолвятся корректно.
        return _load_module("workflow._engine_resume_test", ROOT / "workflow" / "engine.py")
    finally:
        if prev_agent_system is not None:
            sys.modules["agent_system"] = prev_agent_system
        else:
            sys.modules.pop("agent_system", None)


_engine_mod = _load_light_engine()
WorkflowEngine = _engine_mod.WorkflowEngine


def _load_light_state_manager():
    """Загружает state_manager на тех же light-моделях, что и engine.

    В полном прогоне реальный workflow.state_manager уже в sys.modules с РЕАЛЬНЫМИ
    моделями, тогда как engine загружен на light-моделях. Это рассинхронизирует enum
    WorkflowStatus, и checkpoint round-trip (resumable/status) врёт. В проде такого нет —
    весь стек делит один workflow.models; здесь воспроизводим тот же инвариант, загружая
    state_manager под отдельным именем поверх текущих light-моделей.
    """
    return _load_module(
        "workflow._state_manager_resume_test",
        ROOT / "workflow" / "state_manager.py",
    )


# Для round-trip определения (JSON-строки, без enum) достаточно реального state_manager.
_sm_mod = sys.modules["workflow.state_manager"]
SQLiteWorkflowStore = _sm_mod.SQLiteWorkflowStore
WorkflowStateManager = _sm_mod.WorkflowStateManager

# Для checkpoint round-trip нужен state_manager в одном мире enum с engine (см. docstring).
_light_sm_mod = _load_light_state_manager()
LightSQLiteWorkflowStore = _light_sm_mod.SQLiteWorkflowStore
LightWorkflowStateManager = _light_sm_mod.WorkflowStateManager


def _completed(step_id: str) -> StepResult:
    return StepResult(
        step_id=step_id,
        status=StepStatus.COMPLETED,
        start_time=datetime.now(),
        end_time=datetime.now(),
    )


def _failed(step_id: str) -> StepResult:
    return StepResult(
        step_id=step_id,
        status=StepStatus.FAILED,
        start_time=datetime.now(),
        end_time=datetime.now(),
        error="boom",
    )


class TestWorkflowDefinitionPersistence(unittest.TestCase):
    """Task B: round-trip определения через workflow_metadata.definition."""

    def _store(self, tmp: str) -> "SQLiteWorkflowStore":
        return SQLiteWorkflowStore(db_path=str(Path(tmp) / "wf_state.db"))

    def _manager(self, tmp: str) -> "WorkflowStateManager":
        mgr = WorkflowStateManager.__new__(WorkflowStateManager)  # bypass тяжёлый __init__
        mgr.store = self._store(tmp)
        return mgr

    def test_store_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            payload = '{"name": "p", "steps": []}'
            asyncio.run(store.save_workflow_definition("wf1", payload))
            self.assertEqual(asyncio.run(store.get_workflow_definition("wf1")), payload)

    def test_store_missing_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self.assertIsNone(asyncio.run(store.get_workflow_definition("absent")))

    def test_store_upsert_overwrites(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            asyncio.run(store.save_workflow_definition("wf1", '{"v": 1}'))
            asyncio.run(store.save_workflow_definition("wf1", '{"v": 2}'))
            self.assertEqual(asyncio.run(store.get_workflow_definition("wf1")), '{"v": 2}')

    def test_manager_definition_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = self._manager(tmp)
            wf = WorkflowDefinition(
                name="pipe",
                steps=[
                    WorkflowStep(id="a", task="task-a"),
                    WorkflowStep(id="b", task="task-b", depends_on=["a"]),
                ],
            )
            asyncio.run(mgr.save_workflow_definition("wf1", wf))
            restored = asyncio.run(mgr.get_workflow_definition("wf1"))
            self.assertIsNotNone(restored)
            self.assertEqual(restored.name, "pipe")
            self.assertEqual([s.id for s in restored.steps], ["a", "b"])
            self.assertEqual(restored.steps[1].depends_on, ["a"])

    def test_manager_missing_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(asyncio.run(self._manager(tmp).get_workflow_definition("absent")))

    def test_manager_corrupted_json_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = self._manager(tmp)
            asyncio.run(mgr.store.save_workflow_definition("wf1", "{not-json"))
            self.assertIsNone(asyncio.run(mgr.get_workflow_definition("wf1")))


class TestSequentialSkip(unittest.TestCase):
    """Task C: последовательный путь пропускает завершённые шаги, сохраняя их результаты."""

    def _engine(self, executed):
        eng = WorkflowEngine.__new__(WorkflowEngine)
        eng._is_workflow_cancelled = AsyncMock(return_value=False)
        eng._check_step_dependencies = MagicMock(return_value=True)
        eng._should_skip_step_by_condition = MagicMock(return_value=False)
        eng._on_step_completed = AsyncMock()

        async def fake_step(step, context, workflow_def, step_results=None):
            executed.append(step.id)
            return _completed(step.id)

        eng._execute_workflow_step = fake_step
        return eng

    def test_skipped_steps_not_executed_results_preserved(self):
        executed: list[str] = []
        eng = self._engine(executed)
        wf = WorkflowDefinition(
            name="p",
            steps=[
                WorkflowStep(id="a", task="ta"),
                WorkflowStep(id="b", task="tb", depends_on=["a"]),
                WorkflowStep(id="c", task="tc", depends_on=["b"]),
            ],
        )
        ctx = WorkflowContext(workflow_id="wf")
        results = asyncio.run(
            eng._execute_steps_sequential(
                wf, ctx, skip_steps={"a"}, restored_step_results={"a": _completed("a")}
            )
        )
        self.assertEqual(executed, ["b", "c"])  # 'a' пропущен
        self.assertEqual(results["a"].status, StepStatus.COMPLETED)
        self.assertEqual(set(results), {"a", "b", "c"})

    def test_no_skip_is_backward_compatible(self):
        executed: list[str] = []
        eng = self._engine(executed)
        wf = WorkflowDefinition(
            name="p",
            steps=[WorkflowStep(id="a", task="ta"), WorkflowStep(id="b", task="tb")],
        )
        ctx = WorkflowContext(workflow_id="wf")
        results = asyncio.run(eng._execute_steps_sequential(wf, ctx))
        self.assertEqual(executed, ["a", "b"])
        self.assertEqual(set(results), {"a", "b"})

    def test_empty_skip_set_executes_all(self):
        # skip_steps=set() (пустой, но не None) — это путь resume без завершённых шагов:
        # идиома `if skip_steps is not None and step.id in skip_steps` корректно пропускает
        # НИЧЕГО (членство в пустом set всегда False), весь workflow исполняется.
        executed: list[str] = []
        eng = self._engine(executed)
        wf = WorkflowDefinition(
            name="p",
            steps=[WorkflowStep(id="a", task="ta"), WorkflowStep(id="b", task="tb")],
        )
        ctx = WorkflowContext(workflow_id="wf")
        results = asyncio.run(
            eng._execute_steps_sequential(wf, ctx, skip_steps=set(), restored_step_results={})
        )
        self.assertEqual(executed, ["a", "b"])
        self.assertEqual(set(results), {"a", "b"})

    def test_partial_results_published_on_context(self):
        # Корневой фикс: последовательный исполнитель публикует step_results на context,
        # чтобы execute_workflow мог сохранить частичный прогресс в FAILED-checkpoint.
        executed: list[str] = []
        eng = self._engine(executed)
        wf = WorkflowDefinition(name="p", steps=[WorkflowStep(id="a", task="ta")])
        ctx = WorkflowContext(workflow_id="wf")
        asyncio.run(eng._execute_steps_sequential(wf, ctx))
        published = getattr(ctx, "_workflow_step_results", None)
        self.assertIsNotNone(published)
        self.assertIn("a", published)


class TestParallelSkip(unittest.TestCase):
    """Task C: параллельный путь не перезапускает пред-заполненные шаги."""

    def test_preseeded_steps_not_reexecuted(self):
        pe_mod = load_light_parallel_executor()
        ParallelWorkflowExecutor = pe_mod.ParallelWorkflowExecutor

        steps = [
            WorkflowStep(id="a", task="ta"),
            WorkflowStep(id="b", task="tb", depends_on=["a"]),
        ]
        ctx = WorkflowContext(workflow_id="wf")
        executed: list[str] = []

        async def step_executor(step, context):
            executed.append(step.id)
            return _completed(step.id)

        async def run():
            executor = ParallelWorkflowExecutor(max_concurrent=2)
            return await executor.execute_steps_parallel(
                steps,
                ctx,
                step_executor=step_executor,
                dependency_checker=lambda step, results: True,
                condition_checker=lambda step, context: False,
                initial_step_results={"a": _completed("a")},
            )

        results = asyncio.run(run())
        self.assertEqual(executed, ["b"])  # 'a' пред-заполнен и не перезапущен
        self.assertEqual(set(results), {"a", "b"})
        self.assertEqual(results["a"].status, StepStatus.COMPLETED)


class TestResumeWorkflowOrchestration(unittest.TestCase):
    """Task D: WorkflowEngine.resume_workflow — оркестрация и контракт ошибок."""

    def _engine(self, checkpoint, definition):
        eng = WorkflowEngine.__new__(WorkflowEngine)
        eng.state_manager = MagicMock()
        eng.state_manager.store = MagicMock()
        eng.state_manager.store.get_latest_checkpoint = AsyncMock(return_value=checkpoint)
        eng.state_manager.get_workflow_definition = AsyncMock(return_value=definition)
        eng.execute_workflow = AsyncMock(return_value="RESULT")
        return eng

    def _checkpoint(self, *, status, resumable, completed_steps, step_results, context=None):
        return WorkflowCheckpoint(
            workflow_id="wf",
            timestamp=datetime.now(),
            status=status,
            current_step="b",
            completed_steps=completed_steps,
            context=context if context is not None else WorkflowContext(workflow_id="wf"),
            step_results=step_results,
            resumable=resumable,
            metadata={},
        )

    def test_resume_passes_skip_and_filters_to_completed(self):
        ctx = WorkflowContext(workflow_id="wf", step_outputs={"a": {"x": 1}})
        cp = self._checkpoint(
            status=WorkflowStatus.FAILED,
            resumable=True,
            completed_steps=["a"],
            step_results={"a": _completed("a"), "b": _failed("b")},
            context=ctx,
        )
        wf = WorkflowDefinition(
            name="p",
            steps=[WorkflowStep(id="a", task="ta"), WorkflowStep(id="b", task="tb", depends_on=["a"])],
        )
        eng = self._engine(cp, wf)

        result = asyncio.run(eng.resume_workflow("wf"))

        self.assertEqual(result, "RESULT")
        eng.execute_workflow.assert_awaited_once()
        call = eng.execute_workflow.await_args
        self.assertIs(call.args[0], wf)
        self.assertEqual(call.kwargs["skip_steps"], {"a"})
        restored = call.kwargs["restored_step_results"]
        self.assertIn("a", restored)
        self.assertNotIn("b", restored)  # FAILED-шаг не восстанавливается → будет перезапущен

    def test_resume_sets_client_id_on_context(self):
        cp = self._checkpoint(
            status=WorkflowStatus.PAUSED,
            resumable=True,
            completed_steps=[],
            step_results={},
        )
        wf = WorkflowDefinition(name="p", steps=[WorkflowStep(id="a", task="ta")])
        eng = self._engine(cp, wf)

        asyncio.run(eng.resume_workflow("wf", client_id="client-7"))

        ctx = eng.execute_workflow.await_args.kwargs["context"]
        self.assertEqual(ctx.client_id, "client-7")

    def test_resume_uses_context_client_id_when_not_passed(self):
        # WARNING #4: без явного client_id ресурсы должны учитываться на клиента из
        # восстановленного context, а не на None.
        ctx = WorkflowContext(workflow_id="wf", client_id="orig-client")
        cp = self._checkpoint(
            status=WorkflowStatus.FAILED,
            resumable=True,
            completed_steps=[],
            step_results={},
            context=ctx,
        )
        wf = WorkflowDefinition(name="p", steps=[WorkflowStep(id="a", task="ta")])
        eng = self._engine(cp, wf)

        asyncio.run(eng.resume_workflow("wf"))  # client_id не передан

        self.assertEqual(eng.execute_workflow.await_args.kwargs["client_id"], "orig-client")

    def test_resume_no_checkpoint_raises_not_found(self):
        eng = self._engine(None, None)
        with self.assertRaises(WorkflowNotFoundError):
            asyncio.run(eng.resume_workflow("wf"))
        eng.execute_workflow.assert_not_awaited()

    def test_resume_not_resumable_raises(self):
        cp = self._checkpoint(
            status=WorkflowStatus.RUNNING,
            resumable=False,
            completed_steps=[],
            step_results={},
        )
        eng = self._engine(cp, MagicMock())
        with self.assertRaises(WorkflowExecutionError):
            asyncio.run(eng.resume_workflow("wf"))
        eng.execute_workflow.assert_not_awaited()

    def test_resume_missing_definition_raises(self):
        cp = self._checkpoint(
            status=WorkflowStatus.FAILED,
            resumable=True,
            completed_steps=[],
            step_results={},
        )
        eng = self._engine(cp, None)  # get_workflow_definition → None
        with self.assertRaises(WorkflowExecutionError):
            asyncio.run(eng.resume_workflow("wf"))
        eng.execute_workflow.assert_not_awaited()


class TestFailedCheckpointPreservesProgress(unittest.TestCase):
    """Корневой фикс: FAILED-checkpoint сохраняет частичный прогресс (а не пустой словарь),
    благодаря чему resume_workflow перезапускает workflow «с последнего успешного шага»."""

    def _manager(self, tmp: str):
        # Light-менеджер: тот же мир enum, что и engine, иначе resumable round-trip врёт
        # (real-store десериализует в real-WorkflowStatus, а engine шлёт light — см. helper).
        mgr = LightWorkflowStateManager.__new__(LightWorkflowStateManager)  # bypass __init__
        mgr.store = LightSQLiteWorkflowStore(db_path=str(Path(tmp) / "wf.db"))
        mgr.memory_manager = None  # save_checkpoint проверяет атрибут перед записью в память
        return mgr

    def _engine(self, mgr: "WorkflowStateManager") -> "WorkflowEngine":
        eng = WorkflowEngine.__new__(WorkflowEngine)
        eng.state_manager = mgr
        eng._release_workflow_resources = AsyncMock()  # без resource_manager
        return eng

    def _wf(self) -> "WorkflowDefinition":
        return WorkflowDefinition(
            name="p",
            steps=[
                WorkflowStep(id="a", task="ta"),
                WorkflowStep(id="b", task="tb", depends_on=["a"]),
            ],
        )

    def test_failed_checkpoint_persists_partial_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = self._manager(tmp)
            eng = self._engine(mgr)
            ctx = WorkflowContext(workflow_id="wf")
            partial = {"a": _completed("a"), "b": _failed("b")}
            asyncio.run(
                eng._on_workflow_failed(
                    self._wf(), ctx, resource_lease=object(),
                    error=RuntimeError("boom"), step_results=partial,
                )
            )
            cp = asyncio.run(mgr.store.get_latest_checkpoint("wf"))
            self.assertEqual(cp.status, WorkflowStatus.FAILED)
            self.assertTrue(cp.resumable)
            self.assertEqual(cp.completed_steps, ["a"])  # только COMPLETED попадает в skip-список
            self.assertIn("a", cp.step_results)
            self.assertIn("b", cp.step_results)

    def test_failed_checkpoint_without_results_is_empty(self):
        # Обратная совместимость: вызов без step_results не падает и пишет пустой словарь.
        with tempfile.TemporaryDirectory() as tmp:
            mgr = self._manager(tmp)
            eng = self._engine(mgr)
            asyncio.run(
                eng._on_workflow_failed(
                    self._wf(), WorkflowContext(workflow_id="wf"),
                    resource_lease=object(), error=RuntimeError("boom"),
                )
            )
            cp = asyncio.run(mgr.store.get_latest_checkpoint("wf"))
            self.assertEqual(cp.status, WorkflowStatus.FAILED)
            self.assertEqual(cp.completed_steps, [])

    def test_failed_then_resume_skips_completed_step(self):
        # Сквозной сценарий: сбой сохраняет прогресс → resume пропускает завершённый шаг,
        # перезапускает только проваленный (execute_workflow застаблен для изоляции).
        with tempfile.TemporaryDirectory() as tmp:
            mgr = self._manager(tmp)
            eng = self._engine(mgr)
            wf = self._wf()
            ctx = WorkflowContext(workflow_id="wf", step_outputs={"a": {"x": 1}})

            asyncio.run(mgr.save_workflow_definition("wf", wf))
            asyncio.run(
                eng._on_workflow_failed(
                    wf, ctx, resource_lease=object(), error=RuntimeError("boom"),
                    step_results={"a": _completed("a"), "b": _failed("b")},
                )
            )

            eng.execute_workflow = AsyncMock(return_value="OK")
            result = asyncio.run(eng.resume_workflow("wf"))

            self.assertEqual(result, "OK")
            call = eng.execute_workflow.await_args
            self.assertEqual(call.kwargs["skip_steps"], {"a"})
            restored = call.kwargs["restored_step_results"]
            self.assertIn("a", restored)         # завершённый шаг восстановлен
            self.assertNotIn("b", restored)      # проваленный шаг будет перезапущен


if __name__ == "__main__":
    unittest.main()
