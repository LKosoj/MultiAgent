"""Workflow engine and Typed Text-to-SQL pipeline tests."""
from __future__ import annotations

import ast
import asyncio
from contextlib import contextmanager
import importlib.util
import json
import logging
import sys
import types
from pathlib import Path
from urllib.parse import quote_plus

import pytest

from tests.workflow_test_utils import load_light_workflow_models


ROOT = Path(__file__).resolve().parents[1]
_MISSING_MODULE = object()


def _workflow_module_snapshot() -> dict[str, object]:
    return {
        name: module
        for name, module in sys.modules.items()
        if name == "workflow" or name.startswith("workflow.")
    }


def _restore_workflow_module_snapshot(
    snapshot: dict[str, object],
    agent_system: object,
) -> None:
    for name in tuple(sys.modules):
        if name == "workflow" or name.startswith("workflow."):
            sys.modules.pop(name, None)
    sys.modules.update(snapshot)
    if agent_system is _MISSING_MODULE:
        sys.modules.pop("agent_system", None)
    else:
        sys.modules["agent_system"] = agent_system
    for name, module in sorted(
        snapshot.items(),
        key=lambda item: item[0].count("."),
    ):
        parent_name, separator, child = name.rpartition(".")
        parent = snapshot.get(parent_name)
        if separator and parent is not None:
            setattr(parent, child, module)


@contextmanager
def _preserve_workflow_module_tree():
    snapshot = _workflow_module_snapshot()
    agent_system = sys.modules.get("agent_system", _MISSING_MODULE)
    try:
        yield
    finally:
        _restore_workflow_module_snapshot(snapshot, agent_system)


@pytest.fixture(autouse=True)
def _restore_light_workflow_modules():
    with _preserve_workflow_module_tree():
        yield


# ---------------------------------------------------------------------------
# Светлая загрузка workflow.engine (по образцу test_text_to_sql_agui_workflow_contract)
# ---------------------------------------------------------------------------
def _load_module(module_name: str, file_path: Path):
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Failed to load module spec for {module_name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _install_light_workflow_package():
    for module_name in tuple(sys.modules):
        if module_name == "workflow" or module_name.startswith("workflow."):
            sys.modules.pop(module_name, None)
    sys.modules.pop("agent_system", None)

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


def _engine_instance():
    pkg = _install_light_workflow_package()
    return object.__new__(pkg.engine.WorkflowEngine)


def _enhanced_engine_instance():
    pkg = _install_light_workflow_package()
    enhanced_module = _load_module(
        "workflow.enhanced_engine",
        ROOT / "workflow" / "enhanced_engine.py",
    )
    pkg.enhanced_engine = enhanced_module
    return object.__new__(enhanced_module.EnhancedWorkflowEngine)


def _workflow_models():
    return sys.modules["workflow.models"]




def test_enhanced_engine_treats_status_error_dict_as_tool_error():
    engine = _enhanced_engine_instance()
    result = {"status": "error", "message": "provider unavailable"}

    assert engine._is_tool_error_result(result) is True
    assert engine._extract_error_from_result(result) == "provider unavailable"


def test_is_tool_error_result_truthiness_and_narrowed_patterns():
    """WS-A / M-5: истинность значения (не наличие ключа) + сужение строковых паттернов."""
    engine = _enhanced_engine_instance()

    # Falsy error/exception у успешного результата НЕ трактуются как ошибка.
    assert engine._is_tool_error_result({"error": None}) is False
    assert engine._is_tool_error_result({"exception": ""}) is False
    assert engine._is_tool_error_result({"status": "completed"}) is False
    # Truthy error/exception/status=error → ошибка.
    assert engine._is_tool_error_result({"error": "boom"}) is True
    assert engine._is_tool_error_result({"exception": "trace"}) is True
    assert engine._is_tool_error_result({"status": "error"}) is True

    # Сужение M-5: удалённые широкие NL-подстроки больше не сигналят сбой.
    assert engine._is_tool_error_result("Файл не найден пользователем") is False
    assert engine._is_tool_error_result("Отсутствуют исходные данные") is False
    assert engine._is_tool_error_result("Путь не существует на диске") is False
    # Оставшиеся точные паттерны по-прежнему ловятся.
    assert engine._is_tool_error_result("Traceback (most recent call last)") is True
    assert engine._is_tool_error_result("RuntimeError: boom") is True
    assert engine._is_tool_error_result("Ошибка: нет доступа") is True


# ===========================================================================
# 6.1: _substitute_variables_in_metadata
# ===========================================================================
def test_substitute_variables_in_metadata():
    engine = _engine_instance()
    metadata = {
        "max_rows": "{max_rows}",
        "session_id": "{session_id}",
        "run_id": "{run_id}",
        "nested": {
            "dsn": "{dsn}",
            "items": ["{max_rows}", "static"],
        },
        "static_list": ["a", "b"],
    }
    ctx_vars = {
        "max_rows": 100,
        "session_id": "sess-1",
        "run_id": "run-42",
        "dsn": "sqlite:///x.db",
    }

    result = engine._substitute_variables_in_metadata(metadata, ctx_vars, step_id="sql_pipeline")

    # Скалярные значения подставляются с сохранением типа (полная подстановка)
    assert result["max_rows"] == 100
    assert result["session_id"] == "sess-1"
    assert result["run_id"] == "run-42"
    assert result["nested"]["dsn"] == "sqlite:///x.db"
    assert result["nested"]["items"][0] == 100
    assert result["nested"]["items"][1] == "static"
    assert result["static_list"] == ["a", "b"]


def test_metadata_substitution_fail_fast_on_unresolved():
    engine = _engine_instance()
    models = _workflow_models()
    WorkflowExecutionError = models.WorkflowExecutionError

    metadata = {"max_rows": "{max_rows_missing}", "ok": "{session_id}"}
    ctx_vars = {"session_id": "sess-1"}

    with pytest.raises(WorkflowExecutionError, match="Unresolved metadata placeholders"):
        engine._substitute_variables_in_metadata(metadata, ctx_vars, step_id="some_step")


def test_metadata_substitution_allows_braces_inside_substituted_dsn():
    engine = _engine_instance()
    dsn = (
        "Driver={ODBC Driver 17 for SQL Server};"
        "Server=db.example.com;Database=orders;UID=alice;PWD=secret"
    )

    result = engine._substitute_variables_in_metadata(
        {"dsn": "{dsn}"},
        {"dsn": dsn},
        step_id="agent_step",
    )

    assert result["dsn"] == dsn


def test_step_with_substituted_metadata_uses_helper():
    engine = _engine_instance()
    models = _workflow_models()
    WorkflowStep = models.WorkflowStep
    WorkflowContext = models.WorkflowContext

    step = WorkflowStep(
        id="agent_step",
        task="task",
        agent_type="manager",
        metadata={
            "max_rows": "{max_rows}",
            "preload_agents": ["sql_generator_agent"],
        },
    )
    ctx = WorkflowContext(
        workflow_id="wf-x",
        session_id="sess-x",
        variables={"max_rows": 50},
    )

    new_step = engine._step_with_substituted_metadata(step, ctx)
    # Исходный шаг не модифицируется
    assert step.metadata["max_rows"] == "{max_rows}"
    # Новый — со подставленными значениями
    assert new_step.metadata["max_rows"] == 50
    assert new_step.metadata["preload_agents"] == ["sql_generator_agent"]


def test_step_with_substituted_metadata_preserves_output_schema_requirements():
    engine = _engine_instance()
    models = _workflow_models()
    WorkflowStep = models.WorkflowStep
    WorkflowContext = models.WorkflowContext

    requirements = {"required": ["sql", "description"]}
    step = WorkflowStep(
        id="agent_step",
        task="task",
        agent_type="worker_agent",
        metadata={"dsn": "{dsn}"},
        output_schema="json_object",
        output_schema_requirements=requirements,
    )
    ctx = WorkflowContext(
        workflow_id="wf-x",
        session_id="sess-x",
        variables={"dsn": "sqlite:///tmp/app.db"},
    )

    new_step = engine._step_with_substituted_metadata(step, ctx)

    assert new_step.output_schema == "json_object"
    assert new_step.output_schema_requirements == requirements


# ===========================================================================
# 6.2: dict/list values -> json.dumps in task.format
# ===========================================================================
def test_task_format_dict_uses_json_dumps():
    engine = _engine_instance()
    models = _workflow_models()
    WorkflowContext = models.WorkflowContext

    entities = {"metrics": ["revenue"], "dimensions": ["region"]}
    ctx = WorkflowContext(
        workflow_id="wf-1",
        session_id="sess-1",
        variables={"intent_extraction_step": entities},
    )

    formatted = engine._format_task_with_variables(
        "entities={intent_extraction_step}",
        ctx,
        "sql_pipeline",
    )

    # Должно быть JSON, а не str(dict) с одинарными кавычками
    expected_json = json.dumps(entities, ensure_ascii=False)
    assert formatted == f"entities={expected_json}"
    # Защита от регрессии: одинарных кавычек быть не должно
    assert "'metrics'" not in formatted


def test_task_format_list_uses_json_dumps():
    engine = _engine_instance()
    models = _workflow_models()
    WorkflowContext = models.WorkflowContext

    ctx = WorkflowContext(
        workflow_id="wf-1",
        session_id="sess-1",
        variables={"items": ["a", "b", "c"]},
    )
    formatted = engine._format_task_with_variables("items={items}", ctx, "step")
    assert formatted == 'items=["a", "b", "c"]'


def test_task_format_scalar_uses_str():
    engine = _engine_instance()
    models = _workflow_models()
    WorkflowContext = models.WorkflowContext

    ctx = WorkflowContext(
        workflow_id="wf-1",
        session_id="sess-1",
        variables={"max_rows": 100, "safety_level": "strict"},
    )
    formatted = engine._format_task_with_variables(
        "rows={max_rows} level={safety_level}",
        ctx,
        "step",
    )
    assert formatted == "rows=100 level=strict"


def test_task_format_logs_redacted_secret_values(caplog):
    engine = _engine_instance()
    models = _workflow_models()
    WorkflowContext = models.WorkflowContext
    raw_dsn = "postgresql://alice:secret@db.example.com/app?api_key=rawkey"

    ctx = WorkflowContext(
        workflow_id="wf-1",
        session_id="sess-1",
        variables={"dsn": raw_dsn},
    )

    with caplog.at_level(logging.INFO, logger="workflow.engine"):
        formatted = engine._format_task_with_variables("connect {dsn}", ctx, "step")

    assert formatted == f"connect {raw_dsn}"
    assert "alice:secret" not in caplog.text
    assert "rawkey" not in caplog.text
    assert "postgresql://***:***@" in caplog.text


def test_write_step_output_logs_redacted_secret_values(caplog):
    engine = _engine_instance()
    models = _workflow_models()
    WorkflowContext = models.WorkflowContext
    raw_connect = quote_plus(
        "Driver={ODBC Driver 17};Server=db1.example.com;"
        "Database=orders;UID=alice;PWD=topsecret"
    )
    raw_dsn = f"mssql+pyodbc:///?odbc_connect={raw_connect}&driver=ODBC+Driver+17"
    ctx = WorkflowContext(workflow_id="wf-1", session_id="sess-1")

    with caplog.at_level(logging.DEBUG, logger="workflow.engine"):
        engine._write_step_output(ctx, "db_step", {"dsn": raw_dsn})

    assert ctx.step_outputs["db_step.dsn"] == raw_dsn
    assert "alice" not in caplog.text
    assert "topsecret" not in caplog.text
    assert "UID%3D" not in caplog.text
    assert "PWD%3D" not in caplog.text
    assert "odbc_connect=***" in caplog.text


def test_agent_exception_log_redacts_secret_values(caplog):
    engine = _engine_instance()
    models = _workflow_models()
    WorkflowContext = models.WorkflowContext
    WorkflowStep = models.WorkflowStep
    raw_error = "driver failed postgresql://alice:secret@db.example.com/app?api_key=rawkey"

    class Agent:
        def run(self, task, stream=False):
            raise RuntimeError(raw_error)

    class Factory:
        def create_agent(self, **kwargs):
            return Agent()

    class ResourceManager:
        def record_api_call(self, workflow_id):
            pass

    engine.factory = Factory()
    engine.resource_manager = ResourceManager()
    step = WorkflowStep(id="agent", task="t", agent_type="sql_generator_agent")
    context = WorkflowContext(workflow_id="wf-1", session_id="sess-1")

    with caplog.at_level(logging.ERROR, logger="workflow.engine"), pytest.raises(RuntimeError):
        asyncio.run(engine._execute_agent_step(step, context, "task"))

    assert "alice:secret" not in caplog.text
    assert "rawkey" not in caplog.text
    assert "postgresql://***:***@" in caplog.text


def test_agent_max_steps_exhaustion_fails_workflow_step():
    engine = _engine_instance()
    models = _workflow_models()
    WorkflowContext = models.WorkflowContext
    WorkflowStep = models.WorkflowStep

    class AgentMaxStepsError(Exception):
        pass

    class LastMemoryStep:
        error = AgentMaxStepsError("Reached max steps.")

    class Agent:
        memory = types.SimpleNamespace(steps=[LastMemoryStep()])

        def run(self, task, stream=False):
            return "fallback final answer after max steps"

    class Factory:
        def create_agent(self, **kwargs):
            return Agent()

    class ResourceManager:
        def record_api_call(self, workflow_id):
            raise AssertionError("max_steps exhaustion must fail before recording success")

    engine.factory = Factory()
    engine.resource_manager = ResourceManager()
    step = WorkflowStep(id="agent", task="t", agent_type="sql_generator_agent")
    context = WorkflowContext(workflow_id="wf-1", session_id="sess-1")

    with pytest.raises(RuntimeError, match="reached max_steps"):
        asyncio.run(engine._execute_agent_step(step, context, "task"))


def test_tool_exception_log_redacts_secret_values(caplog, monkeypatch):
    engine = _engine_instance()
    models = _workflow_models()
    WorkflowContext = models.WorkflowContext
    WorkflowStep = models.WorkflowStep
    raw_error = "tool failed postgresql://alice:secret@db.example.com/app?api_key=rawkey"

    class Factory:
        tool_mapping = {"schema_info": object()}

        def _create_tool(self, tool_name):
            return lambda **kwargs: None

    class ToolManager:
        def run_tool(self, **kwargs):
            raise RuntimeError(raw_error)

    class ResourceManager:
        def record_api_call(self, workflow_id):
            pass

    engine.factory = Factory()
    engine.resource_manager = ResourceManager()
    monkeypatch.setattr("tool_manager.get_tool_manager", lambda: ToolManager())
    step = WorkflowStep(id="tool", task="t", step_type="tool", tool_name="schema_info")
    context = WorkflowContext(workflow_id="wf-1", session_id="sess-1")

    with caplog.at_level(logging.ERROR, logger="workflow.engine"), pytest.raises(RuntimeError):
        asyncio.run(engine._execute_tool_step(step, context, "task"))

    assert "alice:secret" not in caplog.text
    assert "rawkey" not in caplog.text
    assert "postgresql://***:***@" in caplog.text


def test_workflow_tool_calls_separate_workflow_run_id_from_database_session(monkeypatch):
    engine = _engine_instance()
    models = _workflow_models()
    calls = []

    class ToolManager:
        def run_tool(self, **kwargs):
            calls.append(kwargs)
            return {"ok": True}

    monkeypatch.setattr("tool_manager.get_tool_manager", lambda: ToolManager())
    engine.factory = types.SimpleNamespace(
        _create_tool=lambda _name: (lambda **kwargs: kwargs),
        tool_mapping={"demo": object()},
    )
    engine.resource_manager = types.SimpleNamespace(record_api_call=lambda *_args: None)
    step = models.WorkflowStep(
        id="direct_tool",
        step_type="tool",
        tool_name="demo",
        task="run tool",
        tool_params={"session_id": "{session_id}"},
    )

    for workflow_run_id in ("run-a", "run-b"):
        context = models.WorkflowContext(
            workflow_id=f"workflow-{workflow_run_id}",
            session_id="db-session-shared",
            variables={"run_id": workflow_run_id},
        )
        asyncio.run(engine._execute_tool_step(step, context, step.task))

    assert [call["workflow_run_id"] for call in calls] == ["run-a", "run-b"]
    assert all("tool_invocation_id" not in call for call in calls)
    assert [call["session_id"] for call in calls] == [
        "db-session-shared",
        "db-session-shared",
    ]


def test_tool_manager_uses_unique_invocation_ids_and_keeps_generic_calls_compatible(
    monkeypatch,
):
    tool_manager_module = importlib.import_module("tool_manager")
    starts = []

    class Span:
        def set_attributes(self, _attributes):
            return None

        def set_attribute(self, _key, _value):
            return None

    class Telemetry:
        def start_run_trace(self, **kwargs):
            starts.append(kwargs)
            return Span()

        def finish_run_trace(self, *args, **kwargs):
            return None

    telemetry_module = types.ModuleType("telemetry")
    telemetry_module.get_telemetry_manager = lambda: Telemetry()
    monkeypatch.setitem(sys.modules, "telemetry", telemetry_module)
    manager = tool_manager_module.ToolManager()

    for suffix in ("one", "two"):
        manager.run_tool(
            tool_name=f"generic-{suffix}",
            tool_function=lambda: "ok",
            task_description=f"generic {suffix}",
            session_id="legacy-session",
        )
    manager.run_tool(
        tool_name="workflow-a",
        tool_function=lambda: "ok",
        task_description="workflow a",
        session_id="db-session-shared",
        workflow_run_id="run-shared",
    )
    manager.run_tool(
        tool_name="workflow-b",
        tool_function=lambda: "ok",
        task_description="workflow b",
        session_id="db-session-shared",
        workflow_run_id="run-shared",
    )

    assert len(manager.active_runs) == 4
    assert len(set(manager.active_runs)) == 4
    assert {item["tool_invocation_id"] for item in manager.active_runs.values()} == set(
        manager.active_runs
    )
    generic_runs = [
        item for item in manager.active_runs.values()
        if item["session_id"] == "legacy-session"
    ]
    workflow_runs = [
        item for item in manager.active_runs.values()
        if item["session_id"] == "db-session-shared"
    ]
    assert len(generic_runs) == 2
    assert all(item["workflow_run_id"] is None for item in generic_runs)
    assert len(workflow_runs) == 2
    assert all(item["workflow_run_id"] == "run-shared" for item in workflow_runs)
    assert {item["run_id"] for item in starts} == set(manager.active_runs)


def test_parallel_workflow_tools_and_retry_attempts_get_distinct_invocation_ids(
    monkeypatch,
):
    engine = _engine_instance()
    models = _workflow_models()
    tool_manager_module = importlib.import_module("tool_manager")
    starts = []

    class Span:
        def set_attributes(self, _attributes):
            return None

        def set_attribute(self, _key, _value):
            return None

    class Telemetry:
        def start_run_trace(self, **kwargs):
            starts.append(kwargs)
            return Span()

        def finish_run_trace(self, *args, **kwargs):
            return None

    telemetry_module = types.ModuleType("telemetry")
    telemetry_module.get_telemetry_manager = lambda: Telemetry()
    monkeypatch.setitem(sys.modules, "telemetry", telemetry_module)
    manager = tool_manager_module.ToolManager()
    monkeypatch.setattr("tool_manager.get_tool_manager", lambda: manager)

    engine.factory = types.SimpleNamespace(
        _create_tool=lambda name: (
            lambda session_id=None: {"tool": name, "session_id": session_id}
        ),
        tool_mapping={"parallel-a": object(), "parallel-b": object()},
    )
    engine.resource_manager = types.SimpleNamespace(record_api_call=lambda *_args: None)
    context = models.WorkflowContext(
        workflow_id="workflow-shared",
        session_id="db-session-shared",
        variables={"run_id": "workflow-run-shared"},
    )
    step_a = models.WorkflowStep(
        id="parallel-a",
        step_type="tool",
        tool_name="parallel-a",
        task="parallel a",
    )
    step_b = models.WorkflowStep(
        id="parallel-b",
        step_type="tool",
        tool_name="parallel-b",
        task="parallel b",
    )

    async def execute_calls():
        await asyncio.gather(
            engine._execute_tool_step(step_a, context, step_a.task),
            engine._execute_tool_step(step_b, context, step_b.task),
        )
        await engine._execute_tool_step(step_a, context, step_a.task)

    asyncio.run(execute_calls())

    assert len(manager.active_runs) == 3
    assert len(set(manager.active_runs)) == 3
    assert all(
        item["workflow_run_id"] == "workflow-run-shared"
        for item in manager.active_runs.values()
    )
    assert all(
        item["session_id"] == "db-session-shared"
        for item in manager.active_runs.values()
    )
    assert {item["run_id"] for item in starts} == set(manager.active_runs)


def test_retry_engine_redacts_exception_boundary(caplog):
    workflow_pkg = _install_light_workflow_package()
    retry_engine = workflow_pkg.retry_engine.RetryEngine()
    models = workflow_pkg.models
    raw_error = (
        "driver failed postgresql://alice:secret@db.example.com/app"
        "?api_key=rawkey person@example.com"
    )

    async def fail(context):
        raise RuntimeError(raw_error)

    with caplog.at_level(logging.WARNING, logger="workflow.retry_engine"), pytest.raises(
        models.WorkflowStepError
    ) as exc_info:
        asyncio.run(
            retry_engine.execute_with_retry(
                "secret_step",
                fail,
                {},
                retry_policy=models.RetryPolicy(max_retries=0),
            )
        )

    combined = caplog.text + str(exc_info.value)
    assert "alice:secret" not in combined
    assert "rawkey" not in combined
    assert "person@example.com" not in combined
    assert "postgresql://***:***@" in combined
    assert "[EMAIL]" in combined


def test_adaptive_retry_engine_redacts_failed_step_result(caplog):
    _install_light_workflow_package()
    from workflow.resilience.retry import AdaptiveRetryEngine

    retry_engine = AdaptiveRetryEngine()
    raw_error = (
        "driver failed postgresql://alice:secret@db.example.com/app"
        "?api_key=rawkey person@example.com"
    )

    async def fail(context):
        raise RuntimeError(raw_error)

    with caplog.at_level(logging.WARNING, logger="workflow.resilience.retry"):
        result = asyncio.run(
            retry_engine.execute_with_retry(
                "secret_step",
                fail,
                {},
                max_retries=0,
                base_delay=0,
            )
        )

    serialized = json.dumps({"error": result.error, "metadata": result.metadata}, ensure_ascii=False, default=str)
    combined = caplog.text + serialized
    assert result.status.value == "failed"
    assert "alice:secret" not in combined
    assert "rawkey" not in combined
    assert "person@example.com" not in combined
    assert "postgresql://***:***@" in combined
    assert "[EMAIL]" in combined


# ===========================================================================
# 6.14: run_id substitution
# ===========================================================================
def test_run_id_substitution():
    engine = _engine_instance()
    models = _workflow_models()
    WorkflowContext = models.WorkflowContext

    ctx = WorkflowContext(
        workflow_id="wf-1",
        session_id="sess-1",
        variables={"run_id": "abc"},
    )
    formatted = engine._format_task_with_variables("run={run_id}", ctx, "step")
    assert formatted == "run=abc"


def test_run_id_in_collected_variables():
    """run_id должен быть в _collect_context_variables, даже если приходит через
    parameters → variables (как делает WorkflowManager в service.py)."""
    engine = _engine_instance()
    models = _workflow_models()
    WorkflowContext = models.WorkflowContext

    ctx = WorkflowContext(
        workflow_id="wf-1",
        session_id="sess-1",
        variables={"run_id": "run-zzz", "max_rows": 7},
    )
    collected = engine._collect_context_variables(ctx)
    assert collected["run_id"] == "run-zzz"
    assert collected["session_id"] == "sess-1"
    assert collected["max_rows"] == 7


def test_enhanced_output_retry_recurses_through_retry_policy():
    tree = ast.parse((ROOT / "workflow" / "enhanced_engine.py").read_text(encoding="utf-8"))
    target = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_complete_enhanced_step_with_output_retry":
            target = node
            break
    assert target is not None

    retry_executor = None
    for node in target.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "retry_executor":
            retry_executor = node
            break
    assert retry_executor is not None

    recursive_calls = [
        node
        for node in ast.walk(retry_executor)
        if isinstance(node, ast.Attribute)
        and node.attr == "_complete_enhanced_step_with_output_retry"
    ]
    assert recursive_calls, "enhanced retry must re-enter output_retry_policy handling"


def test_enhanced_non_retryable_step_skips_adaptive_retry():
    engine = _enhanced_engine_instance()
    models = _workflow_models()
    StepStatus = models.StepStatus
    StepResult = models.StepResult
    WorkflowContext = models.WorkflowContext
    WorkflowStep = models.WorkflowStep

    class BudgetManager:
        def create_step_budget(self, step_id):
            return object()

        def consume_budget(self, *args, **kwargs):
            calls["budget_consumed"] += 1

    class CircuitBreakerManager:
        def is_agent_available(self, agent_name):
            return True

        async def call_agent_safely(self, **kwargs):
            calls["attempts"] += 1
            step = kwargs["step"]
            return StepResult(
                step_id=step.id,
                status=StepStatus.FAILED,
                error="audit side effect failed",
            )

    class LoopDetector:
        def is_step_in_loop(self, workflow_id, step_id):
            return False, None

        def record_step_execution(self, workflow_id, step_id, execution_data):
            return False

    class RetryEngine:
        async def execute_with_retry(self, *args, **kwargs):
            raise AssertionError("non-retryable side-effect step must not use adaptive retry")

    calls = {"attempts": 0, "budget_consumed": 0}

    engine.budget_manager = BudgetManager()
    engine.circuit_breaker_manager = CircuitBreakerManager()
    engine.loop_detector = LoopDetector()
    engine.retry_engine = RetryEngine()
    engine.feature_manager = types.SimpleNamespace(is_feature_enabled=lambda *args, **kwargs: False)

    step = WorkflowStep(
        id="db_audit",
        task="audit",
        agent_type="db_audit_agent",
        metadata={"retryable": False},
    )
    context = WorkflowContext(workflow_id="wf-non-retryable", session_id="sess")

    result = asyncio.run(engine._execute_enhanced_step(step, context, {}))

    assert calls["attempts"] == 1
    assert calls["budget_consumed"] == 1
    assert result.status.value == StepStatus.FAILED.value
    assert result.error == "audit side effect failed"


def test_pipeline_yaml_declares_run_id_input():
    """Typed stages share one workflow deadline instead of fixed stage caps."""
    models = load_light_workflow_models()
    workflow = models.WorkflowDefinition.from_yaml(
        ROOT / "workflow_pipelines" / "text_to_sql_pipeline.yaml"
    )
    assert "run_id" in workflow.inputs
    assert workflow.inputs["run_id"] == ""
    assert workflow.global_resource_limits.max_duration_seconds == 14_400
    assert [step.id for step in workflow.steps] == [
        "schema_research",
        "sql_solving",
        "db_audit",
    ]
    assert all(step.timeout is None for step in workflow.steps)
    schema_research = workflow.steps[0]
    assert schema_research.resource_limits.max_duration_seconds == 14_400


def test_skip_output_propagates_status_to_step_outputs():
    """Пропущенный по условию шаг записывает заданный status в step_outputs."""
    engine = _engine_instance()
    models = _workflow_models()
    WorkflowContext = models.WorkflowContext
    WorkflowStep = models.WorkflowStep

    ctx = WorkflowContext(
        workflow_id="wf-1",
        session_id="sess-1",
        variables={"enable_optional_stage": False},
    )
    step = WorkflowStep(
        id="optional_stage",
        task="optional work",
        condition="{enable_optional_stage}",
        metadata={
            "skip_output": {
                "status": "skipped_disabled",
                "reason": "enable_optional_stage=false",
            }
        },
    )

    assert engine._should_skip_step_by_condition(step, ctx) is True
    assert ctx.step_outputs["optional_stage"]["status"] == "skipped_disabled"
    assert ctx.step_outputs["optional_stage.status"] == "skipped_disabled"


# ===========================================================================
# 6.3: декомпозиция god-manager sql_pipeline
# ===========================================================================
def test_enhanced_engine_disabled_fails_for_required_enhanced_workflow():
    engine = _enhanced_engine_instance()
    models = _workflow_models()
    WorkflowDefinition = models.WorkflowDefinition
    WorkflowContext = models.WorkflowContext
    WorkflowExecutionError = models.WorkflowExecutionError

    engine.feature_manager = types.SimpleNamespace(
        is_enhanced_enabled=lambda workflow_id=None: False,
    )
    workflow = WorkflowDefinition(
        name="requires-enhanced",
        steps=[],
        requires_enhanced_engine=True,
    )
    context = WorkflowContext(workflow_id="wf-disabled", session_id="sess-disabled")

    with pytest.raises(WorkflowExecutionError, match="requires enhanced engine"):
        asyncio.run(engine.execute_workflow(workflow, context))


def test_text_to_sql_always_uses_typed_engine_when_generic_feature_is_disabled(
    monkeypatch,
):
    engine = _enhanced_engine_instance()
    models = _workflow_models()
    WorkflowContext = models.WorkflowContext
    WorkflowDefinition = models.WorkflowDefinition
    DeadlineBudget = sys.modules["workflow.deadline"].DeadlineBudget
    expected = object()

    engine.feature_manager = types.SimpleNamespace(
        is_enhanced_enabled=lambda workflow_id=None: False,
    )
    workflow = WorkflowDefinition(
        name="text_to_sql_pipeline",
        steps=[],
        requires_enhanced_engine=True,
        metadata={"category": "text_to_sql"},
    )
    context = WorkflowContext(workflow_id="typed-only", session_id="session")
    context._deadline_budget = DeadlineBudget.from_duration(5.0)

    async def execute_typed(*_args, **_kwargs):
        return expected

    monkeypatch.setattr(engine, "_execute_enhanced_workflow", execute_typed)

    assert asyncio.run(engine.execute_workflow(workflow, context)) is expected


def test_base_engine_rejects_required_enhanced_workflow():
    engine = _engine_instance()
    models = _workflow_models()
    WorkflowDefinition = models.WorkflowDefinition
    WorkflowContext = models.WorkflowContext
    WorkflowExecutionError = models.WorkflowExecutionError

    workflow = WorkflowDefinition(
        name="requires-enhanced",
        steps=[],
        requires_enhanced_engine=True,
    )
    context = WorkflowContext(workflow_id="wf-base", session_id="sess-base")

    with pytest.raises(WorkflowExecutionError, match="requires EnhancedWorkflowEngine"):
        asyncio.run(engine.execute_workflow(workflow, context))


def test_enhanced_engine_required_workflow_disallows_legacy_fallback(monkeypatch):
    engine = _enhanced_engine_instance()
    models = _workflow_models()
    WorkflowDefinition = models.WorkflowDefinition
    WorkflowContext = models.WorkflowContext
    WorkflowExecutionError = models.WorkflowExecutionError

    engine.feature_manager = types.SimpleNamespace(
        is_enhanced_enabled=lambda workflow_id=None: True,
        global_config={"enhanced_workflow": {"fallback_to_legacy": True}},
        workflow_overrides={},
    )
    workflow = WorkflowDefinition(
        name="requires-enhanced",
        steps=[],
        requires_enhanced_engine=True,
    )
    context = WorkflowContext(workflow_id="wf-fallback", session_id="sess-fallback")

    async def fail_enhanced(*_args, **_kwargs):
        raise RuntimeError("enhanced failed")

    monkeypatch.setattr(engine, "_execute_enhanced_workflow", fail_enhanced)

    with pytest.raises(WorkflowExecutionError, match="legacy fallback is not allowed"):
        asyncio.run(engine.execute_workflow(workflow, context))


def test_enhanced_engine_required_workflow_resume_uses_enhanced():
    """WS-A / M-3: resume (skip_steps задан) для requires_enhanced_engine пайплайна
    исполняется enhanced-движком, а НЕ бросает и НЕ деградирует в legacy."""
    from datetime import datetime as _dt

    engine = _enhanced_engine_instance()
    models = _workflow_models()
    WorkflowDefinition = models.WorkflowDefinition
    WorkflowContext = models.WorkflowContext
    WorkflowResult = models.WorkflowResult
    WorkflowStatus = models.WorkflowStatus
    StepResult = models.StepResult
    StepStatus = models.StepStatus

    workflow = WorkflowDefinition(
        name="requires-enhanced",
        steps=[],
        requires_enhanced_engine=True,
    )
    context = WorkflowContext(workflow_id="wf-resume", session_id="sess-resume")
    restored = {"s1": StepResult(step_id="s1", status=StepStatus.COMPLETED, output={"ok": True})}
    captured = {}

    async def fake_enhanced(workflow_def, ctx, client_id=None, *, skip_steps=None,
                            restored_step_results=None):
        captured["skip_steps"] = skip_steps
        captured["restored"] = restored_step_results
        return WorkflowResult(
            workflow_id=ctx.workflow_id,
            status=WorkflowStatus.COMPLETED,
            start_time=_dt.now(),
        )

    engine._execute_enhanced_workflow = fake_enhanced

    result = asyncio.run(
        engine.execute_workflow(
            workflow, context, skip_steps={"s1"}, restored_step_results=restored,
        )
    )

    assert result.status == WorkflowStatus.COMPLETED
    assert captured["skip_steps"] == {"s1"}
    assert captured["restored"] is restored
