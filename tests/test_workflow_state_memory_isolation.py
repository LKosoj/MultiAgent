import asyncio
import sys
import threading
from types import ModuleType, SimpleNamespace

import pytest

import memory.manager as memory_manager_module
from workflow.models import WorkflowContext, WorkflowStatus
from workflow.result_repository import load_reconciled_workflow_result
from workflow.state_manager import SQLiteWorkflowStore, WorkflowStateManager
import workflow.state_manager as state_manager_module


def _manager_with_store(monkeypatch, tmp_path, memory_factory):
    memory_module = ModuleType("memory.manager")
    memory_module.get_memory_manager = memory_factory
    monkeypatch.setitem(sys.modules, "memory.manager", memory_module)
    store = SQLiteWorkflowStore(str(tmp_path / "workflow_state.db"))
    monkeypatch.setattr(state_manager_module, "SQLiteWorkflowStore", lambda: store)
    monkeypatch.setattr(
        WorkflowStateManager, "_maybe_cleanup_state", lambda *_args, **_kwargs: None
    )
    return WorkflowStateManager(), store


def test_memory_constructor_failure_keeps_checkpoint_and_result_paths_available(
    monkeypatch, tmp_path
):
    def fail_memory_constructor():
        raise RuntimeError("vector memory constructor failed")

    manager, _store = _manager_with_store(
        monkeypatch, tmp_path, fail_memory_constructor
    )
    context = WorkflowContext(workflow_id="constructor-failure")

    asyncio.run(
        manager.save_checkpoint(
            context.workflow_id,
            WorkflowStatus.FAILED,
            context,
            {},
        )
    )

    assert manager.memory_manager is None
    assert (
        asyncio.run(manager.resume_workflow(context.workflow_id)).workflow_id
        == context.workflow_id
    )

    primary_store = SimpleNamespace(
        list_after=lambda _run_id, _after_seq: [
            SimpleNamespace(
                event_type="WORKFLOW_RESULT",
                run_id=context.workflow_id,
                payload={"run_id": context.workflow_id, "status": "completed"},
                created_at_ms=1,
            )
        ]
    )
    result = load_reconciled_workflow_result(
        context.workflow_id,
        primary_store=primary_store,
    )
    assert result is not None
    assert result.payload["status"] == "completed"


def test_vector_rebuild_failure_after_durable_commit_does_not_lose_checkpoint(
    monkeypatch, tmp_path
):
    manager, store = _manager_with_store(monkeypatch, tmp_path, lambda: object())

    async def fail_rebuild(_checkpoint):
        raise RuntimeError("vector rebuild failed")

    monkeypatch.setattr(manager, "_save_to_memory_system", fail_rebuild)
    context = WorkflowContext(workflow_id="rebuild-failure")

    asyncio.run(
        manager.save_checkpoint(
            context.workflow_id,
            WorkflowStatus.FAILED,
            context,
            {},
        )
    )

    checkpoint = asyncio.run(store.get_latest_checkpoint(context.workflow_id))
    assert checkpoint is not None
    assert checkpoint.workflow_id == context.workflow_id


def test_crash_after_checkpoint_leaves_durable_checkpoint_readable(
    monkeypatch, tmp_path
):
    manager, store = _manager_with_store(monkeypatch, tmp_path, lambda: object())

    async def crash_vector_memory(_checkpoint):
        raise SystemExit("vector memory process stopped")

    monkeypatch.setattr(manager, "_save_to_memory_system", crash_vector_memory)
    context = WorkflowContext(workflow_id="crash-after-checkpoint")

    with pytest.raises(SystemExit, match="vector memory process stopped"):
        asyncio.run(
            manager.save_checkpoint(
                context.workflow_id,
                WorkflowStatus.FAILED,
                context,
                {},
            )
        )

    fresh_store = SQLiteWorkflowStore(store.db_path)
    checkpoint = asyncio.run(fresh_store.get_latest_checkpoint(context.workflow_id))
    assert checkpoint is not None
    assert checkpoint.workflow_id == context.workflow_id


def test_concurrent_memory_manager_initialization_creates_one_shared_instance(
    monkeypatch,
):
    created = 0
    creation_lock = threading.Lock()
    first_constructor_started = threading.Event()
    release_first_constructor = threading.Event()
    second_constructor_started = threading.Event()

    class FakeMemoryManager:
        def __init__(self):
            nonlocal created
            with creation_lock:
                created += 1
                current = created
            if current == 1:
                first_constructor_started.set()
                assert release_first_constructor.wait(timeout=2)
            else:
                second_constructor_started.set()

    monkeypatch.setattr(memory_manager_module, "MemoryManager", FakeMemoryManager)
    monkeypatch.setattr(memory_manager_module, "_global_memory_manager", None)

    instances = []
    errors = []

    def initialize():
        try:
            instances.append(memory_manager_module.get_memory_manager())
        except BaseException as error:
            errors.append(error)

    first = threading.Thread(target=initialize)
    second = threading.Thread(target=initialize)
    first.start()
    assert first_constructor_started.wait(timeout=2)
    second.start()
    second_started_early = second_constructor_started.wait(timeout=0.2)
    release_first_constructor.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert not errors
    assert not second_started_early
    assert created == 1
    assert instances[0] is instances[1]
