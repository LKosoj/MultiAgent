"""EPIC 6 Block A: workflow engine + pipeline yaml.

Тесты соответствуют задачам:
    6.1   _substitute_variables_in_metadata + fail-fast на unresolved
    6.2   json.dumps для dict/list при подстановке в task.format
    6.14  run_id подставляется наравне с session_id
    6.9   schema_linking_step.metadata.skip_output: status=skipped_disabled (вместо disabled: true)
    6.3   декомпозиция god-manager: sql_generation -> sql_verification -> db_audit
"""
from __future__ import annotations

import ast
import asyncio
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


def _verification_output(status: str, recommendations: list[str] | None = None) -> dict:
    return {
        "verification_status": status,
        "safety_check": "ok" if status == "Approved" else "failed",
        "performance_check": "ok",
        "recommendations": recommendations or [],
    }


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
        step_id="sql_generation",
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


def test_pipeline_yaml_declares_run_id_input():
    """run_id должен быть объявлен в inputs пайплайна как пустая строка (как session_id)."""
    models = load_light_workflow_models()
    workflow = models.WorkflowDefinition.from_yaml(
        ROOT / "workflow_pipelines" / "text_to_sql_pipeline.yaml"
    )
    assert "run_id" in workflow.inputs
    assert workflow.inputs["run_id"] == ""


# ===========================================================================
# 6.9: schema_linking_step skip_output uses status: "skipped_disabled"
# ===========================================================================
def test_status_skipped_disabled_instead_of_disabled_true():
    models = load_light_workflow_models()
    workflow = models.WorkflowDefinition.from_yaml(
        ROOT / "workflow_pipelines" / "text_to_sql_pipeline.yaml"
    )
    schema_step = next(s for s in workflow.steps if s.id == "schema_linking_step")
    skip_output = schema_step.metadata["skip_output"]

    assert skip_output.get("status") == "skipped_disabled"
    assert "disabled" not in skip_output, (
        "Старый ключ disabled должен быть полностью убран, чтобы не путать downstream"
    )


def test_skip_output_propagates_status_to_step_outputs():
    """Когда условие false и schema_linking_step скиппится, в step_outputs появляется
    status=skipped_disabled (это новый контракт после 6.9)."""
    engine = _engine_instance()
    models = _workflow_models()
    WorkflowContext = models.WorkflowContext
    WorkflowStep = models.WorkflowStep

    ctx = WorkflowContext(
        workflow_id="wf-1",
        session_id="sess-1",
        variables={"use_schema_suggestions": False},
    )
    step = WorkflowStep(
        id="schema_linking_step",
        task="schema linking",
        condition="{use_schema_suggestions}",
        metadata={
            "skip_output": {
                "status": "skipped_disabled",
                "reason": "use_schema_suggestions=false",
            }
        },
    )

    assert engine._should_skip_step_by_condition(step, ctx) is True
    assert ctx.step_outputs["schema_linking_step"]["status"] == "skipped_disabled"
    assert ctx.step_outputs["schema_linking_step.status"] == "skipped_disabled"


# ===========================================================================
# 6.3: декомпозиция god-manager sql_pipeline
# ===========================================================================
def _load_text_to_sql_workflow():
    models = load_light_workflow_models()
    return models.WorkflowDefinition.from_yaml(
        ROOT / "workflow_pipelines" / "text_to_sql_pipeline.yaml"
    )


def test_pipeline_has_3_decomposed_steps_sql_gen_verify_audit():
    workflow = _load_text_to_sql_workflow()
    step_ids = [s.id for s in workflow.steps]

    # sql_pipeline god-step должен быть удалён
    assert "sql_pipeline" not in step_ids, (
        "После декомпозиции (6.3) шаг 'sql_pipeline' должен быть удалён"
    )

    # Должны появиться три новых шага
    assert "sql_generation" in step_ids
    assert "sql_verification" in step_ids
    assert "db_audit" in step_ids

    # Каждый шаг — отдельный агент
    gen_step = next(s for s in workflow.steps if s.id == "sql_generation")
    ver_step = next(s for s in workflow.steps if s.id == "sql_verification")
    aud_step = next(s for s in workflow.steps if s.id == "db_audit")

    assert gen_step.step_type == "agent"
    assert gen_step.agent_type == "sql_generator_agent"
    assert ver_step.agent_type == "sql_verifier_agent"
    assert aud_step.agent_type == "db_audit_agent"

    # depends_on выстраивается в цепочку
    assert "schema_linking_step" in gen_step.depends_on
    assert "sql_generation" in ver_step.depends_on
    assert "sql_verification" in aud_step.depends_on


def test_verifier_reject_triggers_generator_retry():
    """Runtime тест: при verification_status=Rejected движок РЕАЛЬНО запускает
    sql_generation повторно через output_retry_policy.

    Проверяется через мок retry_engine: считаем число вызовов sql_generation
    при условии, что первая верификация возвращает Rejected, вторая — Approved.
    """
    engine = _engine_instance()
    workflow = _load_text_to_sql_workflow()
    models = _workflow_models()
    StepStatus = models.StepStatus
    StepResult = models.StepResult
    WorkflowContext = models.WorkflowContext

    gen_step = next(s for s in workflow.steps if s.id == "sql_generation")
    ver_step = next(s for s in workflow.steps if s.id == "sql_verification")

    # output_retry_policy должен быть распарсен из YAML
    assert ver_step.output_retry_policy is not None, (
        "sql_verification должен иметь output_retry_policy для feedback loop"
    )
    policy = ver_step.output_retry_policy
    assert policy["rerun_step"] == "sql_generation"
    assert policy["feedback_field"] == "sql_safety_check_feedback"
    assert policy["max_iterations"] >= 1

    # Готовим контекст с обязательными переменными (нужно для metadata-substitution).
    ctx = WorkflowContext(
        workflow_id="wf-retry",
        session_id="sess-retry",
        variables={
            "query": "test",
            "dsn": "sqlite:///x.db",
            "max_rows": 10,
            "session_id": "sess-retry",
            "run_id": "run-retry",
            "safety_level": "strict",
            "include_explanation": True,
            "validate_schema": True,
            "dry_run_only": False,
            "use_schema_suggestions": True,
            "allow_enhanced_fallback": False,
        },
    )

    # Мокаем retry_engine.execute_with_retry: возвращает заранее заданный output
    # в зависимости от step_id и количества вызовов.
    call_counter = {"sql_generation": 0, "sql_verification": 0}

    async def fake_execute_with_retry(step_id, step_func, context, retry_policy=None):
        call_counter[step_id] = call_counter.get(step_id, 0) + 1
        if step_id == "sql_generation":
            output = {"sql": f"SELECT 1 -- v{call_counter[step_id]}", "description": "x"}
        elif step_id == "sql_verification":
            # Первый раз — Rejected, второй — Approved
            if call_counter[step_id] == 1:
                output = _verification_output("Rejected", ["fix WHERE"])
            else:
                output = _verification_output("Approved")
        else:
            output = {}
        from datetime import datetime as _dt
        return StepResult(
            step_id=step_id,
            status=StepStatus.COMPLETED,
            output=output,
            start_time=_dt.now(),
            end_time=_dt.now(),
        )

    class _RetryEngine:
        async def execute_with_retry(self, *args, **kwargs):
            return await fake_execute_with_retry(*args, **kwargs)

    engine.retry_engine = _RetryEngine()
    # state_manager не нужен — _on_step_completed внутри _maybe_run_output_retry
    # сохраняет checkpoint, замокаем no-op.

    checkpoints = []

    class _StateManager:
        async def save_checkpoint(self, **kwargs):
            checkpoints.append({
                "current_step": kwargs.get("current_step"),
                "step_result_keys": set((kwargs.get("step_results") or {}).keys()),
            })
            return None

    engine.state_manager = _StateManager()

    # Запускаем verification — _maybe_run_output_retry внутри обнаружит Rejected
    # и перезапустит sql_generation, затем повторно sql_verification.
    step_results = {
        gen_step.id: StepResult(
            step_id=gen_step.id,
            status=StepStatus.COMPLETED,
            output={"sql": "SELECT old", "description": "stale"},
        )
    }
    result = asyncio.run(
        engine._execute_workflow_step(ver_step, ctx, workflow, step_results=step_results)
    )

    # 1) sql_generation должен быть вызван дважды (1 раз rerun)
    assert call_counter["sql_generation"] == 1, (
        f"sql_generation должен ЗАПУСКАТЬСЯ повторно при Rejected, "
        f"вызовов: {call_counter['sql_generation']}"
    )
    # NB: первый вызов sql_generation делается отдельно как зависимость, в этом
    # тесте мы вызываем только verification напрямую, поэтому 1 = один rerun.
    # 2) sql_verification должен быть вызван дважды (1 раз изначально, 1 раз после rerun)
    assert call_counter["sql_verification"] == 2, (
        f"sql_verification должен быть вызван дважды (initial + после rerun), "
        f"вызовов: {call_counter['sql_verification']}"
    )
    # 3) Финальный output verification — Approved
    assert result.output["verification_status"] == "Approved"
    assert ctx.step_outputs["sql_verification"]["verification_status"] == "Approved"
    assert ctx.step_outputs["sql_verification.verification_status"] == "Approved"
    assert ctx.step_outputs["sql_verification.recommendations"] == []
    # 4) step_results должен видеть свежий output rerun-step; финальные artifacts
    # строятся из step_results, а не только из context.step_outputs.
    assert step_results["sql_generation"].output["sql"] == "SELECT 1 -- v1"


def test_enhanced_output_retry_updates_context_on_success():
    """Enhanced runtime должен оставить в context свежий Approved после retry."""
    engine = _enhanced_engine_instance()
    workflow = _load_text_to_sql_workflow()
    models = _workflow_models()
    StepStatus = models.StepStatus
    StepResult = models.StepResult
    WorkflowContext = models.WorkflowContext

    gen_step = next(s for s in workflow.steps if s.id == "sql_generation")
    ver_step = next(s for s in workflow.steps if s.id == "sql_verification")
    ctx = WorkflowContext(
        workflow_id="wf-enhanced-retry",
        session_id="sess-enhanced-retry",
        variables={
            "query": "test",
            "dsn": "sqlite:///x.db",
            "max_rows": 10,
            "session_id": "sess-enhanced-retry",
            "run_id": "run-enhanced-retry",
            "safety_level": "strict",
            "include_explanation": True,
            "validate_schema": True,
            "dry_run_only": False,
            "use_schema_suggestions": True,
            "allow_enhanced_fallback": False,
        },
    )

    checkpoints = []

    class _StateManager:
        async def save_checkpoint(self, **kwargs):
            checkpoints.append({
                "current_step": kwargs.get("current_step"),
                "step_result_keys": set((kwargs.get("step_results") or {}).keys()),
            })
            return None

    engine.state_manager = _StateManager()
    calls = {"sql_generation": 0, "sql_verification": 0}

    async def fake_execute_enhanced_step(step, context, previous_results):
        from datetime import datetime as _dt

        calls[step.id] = calls.get(step.id, 0) + 1
        if step.id == "sql_generation":
            output = {"sql": "SELECT 1 -- enhanced", "description": "fresh"}
            status = StepStatus.COMPLETED
            error = None
        elif step.id == "sql_verification":
            output = _verification_output("Approved")
            status = StepStatus.COMPLETED
            error = None
        else:
            output = {}
            status = StepStatus.COMPLETED
            error = None
        return StepResult(
            step_id=step.id,
            status=status,
            output=output,
            error=error,
            start_time=_dt.now(),
            end_time=_dt.now(),
        )

    engine._execute_enhanced_step = fake_execute_enhanced_step
    step_results = {
        gen_step.id: StepResult(
            step_id=gen_step.id,
            status=StepStatus.COMPLETED,
            output={"sql": "SELECT old", "description": "stale"},
        )
    }
    initial = StepResult(
        step_id=ver_step.id,
        status=StepStatus.COMPLETED,
        output=_verification_output("Rejected", ["fix"]),
    )

    result = asyncio.run(
        engine._complete_enhanced_step_with_output_retry(
            ver_step, initial, ctx, workflow, step_results
        )
    )

    assert result.output["verification_status"] == "Approved"
    assert calls["sql_generation"] == 1
    assert calls["sql_verification"] == 1
    assert ctx.step_outputs["sql_verification"]["verification_status"] == "Approved"
    assert ctx.step_outputs["sql_verification.verification_status"] == "Approved"
    assert step_results["sql_generation"].output["sql"] == "SELECT 1 -- enhanced"
    generation_checkpoints = [
        item for item in checkpoints if item["current_step"] == "sql_generation"
    ]
    assert len(generation_checkpoints) == 1
    assert "sql_generation" in generation_checkpoints[0]["step_result_keys"]


def test_enhanced_parallel_wrapper_passes_and_updates_shared_step_results():
    engine = _enhanced_engine_instance()
    models = _workflow_models()
    StepStatus = models.StepStatus
    StepResult = models.StepResult
    WorkflowContext = models.WorkflowContext
    WorkflowStep = models.WorkflowStep
    WorkflowDefinition = models.WorkflowDefinition

    step = WorkflowStep(id="current", task="t", agent_type="x")
    previous = StepResult(
        step_id="previous",
        status=StepStatus.COMPLETED,
        output={"value": 0},
    )
    initial = StepResult(
        step_id="current",
        status=StepStatus.COMPLETED,
        output={"value": 1},
    )
    completed = StepResult(
        step_id="current",
        status=StepStatus.COMPLETED,
        output={"value": 2},
    )
    shared_results = {"previous": previous}
    context = WorkflowContext(workflow_id="wf-enhanced-parallel")
    context._workflow_definition = WorkflowDefinition(name="wf-enhanced-parallel", steps=[step])
    context._workflow_step_results = shared_results
    captured = {}

    async def fake_execute_enhanced_step(step_arg, context_arg, previous_results):
        captured["execute_previous_results"] = previous_results
        return initial

    async def fake_complete(step_arg, step_result_arg, context_arg, workflow_def_arg, step_results_arg):
        captured["complete_step_results"] = step_results_arg
        captured["complete_current_result"] = step_results_arg.get(step.id)
        return completed

    engine._execute_enhanced_step = fake_execute_enhanced_step
    engine._complete_enhanced_step_with_output_retry = fake_complete

    result = asyncio.run(engine._execute_enhanced_step_wrapper(step, context))

    assert result is completed
    assert captured["execute_previous_results"] is shared_results
    assert captured["complete_step_results"] is shared_results
    assert captured["complete_current_result"] is initial
    assert shared_results["previous"] is previous
    assert shared_results["current"] is completed


def test_enhanced_output_retry_failed_recheck_does_not_keep_rejected_context():
    """Если повторный verifier FAILED, старый Rejected не должен выглядеть финальным output."""
    engine = _enhanced_engine_instance()
    workflow = _load_text_to_sql_workflow()
    models = _workflow_models()
    StepStatus = models.StepStatus
    StepResult = models.StepResult
    WorkflowContext = models.WorkflowContext

    ver_step = next(s for s in workflow.steps if s.id == "sql_verification")
    ctx = WorkflowContext(
        workflow_id="wf-enhanced-retry-failed",
        session_id="sess-enhanced-retry-failed",
        variables={
            "query": "test",
            "dsn": "sqlite:///x.db",
            "max_rows": 10,
            "session_id": "sess-enhanced-retry-failed",
            "run_id": "run-enhanced-retry-failed",
            "safety_level": "strict",
            "include_explanation": True,
            "validate_schema": True,
            "dry_run_only": False,
            "use_schema_suggestions": True,
            "allow_enhanced_fallback": False,
        },
    )

    class _StateManager:
        async def save_checkpoint(self, **kwargs):
            return None

    engine.state_manager = _StateManager()

    async def fake_execute_enhanced_step(step, context, previous_results):
        from datetime import datetime as _dt

        if step.id == "sql_generation":
            return StepResult(
                step_id=step.id,
                status=StepStatus.COMPLETED,
                output={"sql": "SELECT 1 -- enhanced", "description": "fresh"},
                start_time=_dt.now(),
                end_time=_dt.now(),
            )
        return StepResult(
            step_id=step.id,
            status=StepStatus.FAILED,
            output=None,
            error="verifier failed",
            start_time=_dt.now(),
            end_time=_dt.now(),
        )

    engine._execute_enhanced_step = fake_execute_enhanced_step
    initial = StepResult(
        step_id=ver_step.id,
        status=StepStatus.COMPLETED,
        output=_verification_output("Rejected", ["fix"]),
    )

    result = asyncio.run(
        engine._complete_enhanced_step_with_output_retry(
            ver_step, initial, ctx, workflow, {}
        )
    )

    assert result.status == StepStatus.FAILED
    assert "sql_verification" not in ctx.step_outputs
    assert "sql_verification.verification_status" not in ctx.step_outputs


def test_db_audit_skipped_when_verifier_rejects():
    """db_audit имеет condition '{sql_verification.verification_status} == "Approved"'.
    Если verifier вернул Rejected, db_audit должен скипнуться и подставить skip_output.
    """
    engine = _engine_instance()
    workflow = _load_text_to_sql_workflow()
    models = _workflow_models()
    WorkflowContext = models.WorkflowContext

    audit_step = next(s for s in workflow.steps if s.id == "db_audit")

    # db_audit должен иметь condition
    assert audit_step.condition is not None
    assert "Approved" in audit_step.condition

    ctx = WorkflowContext(
        workflow_id="wf-1",
        session_id="sess-1",
        variables={},
        step_outputs={
            "sql_verification": _verification_output("Rejected", ["fix"]),
            "sql_verification.verification_status": "Rejected",
        },
    )

    # _should_skip_step_by_condition должен вернуть True (condition не выполнено)
    assert engine._should_skip_step_by_condition(audit_step, ctx) is True

    # skip_output должен быть подставлен в step_outputs
    audit_out = ctx.step_outputs.get("db_audit")
    assert isinstance(audit_out, dict)
    assert audit_out.get("status") == "skipped_rejected_by_verifier"
    assert audit_out.get("executed") is False
    # И dotted-ключ тоже
    assert ctx.step_outputs.get("db_audit.status") == "skipped_rejected_by_verifier"


def test_db_audit_runs_when_verifier_approves():
    """db_audit должен НЕ скипаться, если verifier вернул Approved."""
    engine = _engine_instance()
    workflow = _load_text_to_sql_workflow()
    models = _workflow_models()
    WorkflowContext = models.WorkflowContext

    audit_step = next(s for s in workflow.steps if s.id == "db_audit")

    ctx = WorkflowContext(
        workflow_id="wf-1",
        session_id="sess-1",
        variables={},
        step_outputs={
            "sql_verification": {"verification_status": "Approved"},
            "sql_verification.verification_status": "Approved",
        },
    )

    # condition == "Approved" → выполнено → шаг НЕ скипается
    assert engine._should_skip_step_by_condition(audit_step, ctx) is False
    # step_outputs.db_audit не должен быть подставлен (skip_output не сработал)
    assert "db_audit" not in ctx.step_outputs


def test_feedback_propagated_to_sql_generation_on_retry():
    """При retry feedback с recommendations должен попасть в
    context.variables[feedback_field] (sql_safety_check_feedback), чтобы
    sql_generator_agent мог его подставить через {sql_safety_check_feedback}.
    """
    engine = _engine_instance()
    workflow = _load_text_to_sql_workflow()
    models = _workflow_models()
    StepStatus = models.StepStatus
    StepResult = models.StepResult
    WorkflowContext = models.WorkflowContext

    ver_step = next(s for s in workflow.steps if s.id == "sql_verification")

    ctx = WorkflowContext(
        workflow_id="wf-fb",
        session_id="sess-fb",
        variables={
            "query": "test",
            "dsn": "sqlite:///x.db",
            "max_rows": 10,
            "session_id": "sess-fb",
            "run_id": "run-fb",
            "safety_level": "strict",
            "include_explanation": True,
            "validate_schema": True,
            "dry_run_only": False,
            "use_schema_suggestions": True,
            "allow_enhanced_fallback": False,
        },
    )

    seen_variables_at_rerun = {}
    call_counter = {"sql_generation": 0, "sql_verification": 0}

    async def fake_execute_with_retry(step_id, step_func, context, retry_policy=None):
        call_counter[step_id] = call_counter.get(step_id, 0) + 1
        from datetime import datetime as _dt
        if step_id == "sql_generation":
            # На rerun сохраняем snapshot ctx.variables, чтобы проверить feedback
            seen_variables_at_rerun["call_" + str(call_counter[step_id])] = (
                dict(ctx.variables)
            )
            output = {"sql": "SELECT 1", "description": "x"}
        else:  # sql_verification
            if call_counter[step_id] == 1:
                output = _verification_output("Rejected", ["use WHERE", "limit rows"])
            else:
                output = _verification_output("Approved")
        return StepResult(
            step_id=step_id,
            status=StepStatus.COMPLETED,
            output=output,
            start_time=_dt.now(),
            end_time=_dt.now(),
        )

    class _RetryEngine:
        async def execute_with_retry(self, *args, **kwargs):
            return await fake_execute_with_retry(*args, **kwargs)

    class _StateManager:
        async def save_checkpoint(self, **kwargs):
            return None

    engine.retry_engine = _RetryEngine()
    engine.state_manager = _StateManager()

    asyncio.run(engine._execute_workflow_step(ver_step, ctx, workflow))

    # На rerun (call_1) sql_generation должен видеть sql_safety_check_feedback
    # в context.variables
    assert "call_1" in seen_variables_at_rerun
    fb_vars = seen_variables_at_rerun["call_1"]
    assert "sql_safety_check_feedback" in fb_vars, (
        "При rerun sql_generation context.variables должен содержать "
        "sql_safety_check_feedback с output отклонённой верификации"
    )
    feedback_value = fb_vars["sql_safety_check_feedback"]
    assert "Rejected" in feedback_value
    assert "use WHERE" in feedback_value


def test_output_retry_policy_respects_max_iterations():
    """Loop guard: если verifier продолжает возвращать Rejected, движок
    останавливается после max_iterations. Финальный output остаётся Rejected,
    db_audit потом скипнется по condition.
    """
    engine = _engine_instance()
    workflow = _load_text_to_sql_workflow()
    models = _workflow_models()
    StepStatus = models.StepStatus
    StepResult = models.StepResult
    WorkflowContext = models.WorkflowContext

    ver_step = next(s for s in workflow.steps if s.id == "sql_verification")
    max_iter = ver_step.output_retry_policy["max_iterations"]

    ctx = WorkflowContext(
        workflow_id="wf-loop",
        session_id="sess-loop",
        variables={
            "query": "test",
            "dsn": "sqlite:///x.db",
            "max_rows": 10,
            "session_id": "sess-loop",
            "run_id": "run-loop",
            "safety_level": "strict",
            "include_explanation": True,
            "validate_schema": True,
            "dry_run_only": False,
            "use_schema_suggestions": True,
            "allow_enhanced_fallback": False,
        },
    )

    call_counter = {"sql_generation": 0, "sql_verification": 0}

    async def fake_execute_with_retry(step_id, step_func, context, retry_policy=None):
        call_counter[step_id] = call_counter.get(step_id, 0) + 1
        from datetime import datetime as _dt
        if step_id == "sql_generation":
            output = {"sql": "SELECT 1", "description": "x"}
        else:
            # Всегда Rejected
            output = _verification_output("Rejected", ["fix"])
        return StepResult(
            step_id=step_id,
            status=StepStatus.COMPLETED,
            output=output,
            start_time=_dt.now(),
            end_time=_dt.now(),
        )

    class _RetryEngine:
        async def execute_with_retry(self, *args, **kwargs):
            return await fake_execute_with_retry(*args, **kwargs)

    class _StateManager:
        async def save_checkpoint(self, **kwargs):
            return None

    engine.retry_engine = _RetryEngine()
    engine.state_manager = _StateManager()

    result = asyncio.run(engine._execute_workflow_step(ver_step, ctx, workflow))

    # sql_generation должно быть запущено ровно max_iter раз (только rerun'ы)
    assert call_counter["sql_generation"] == max_iter, (
        f"Ожидалось {max_iter} rerun'ов sql_generation, получено {call_counter['sql_generation']}"
    )
    # sql_verification: 1 первоначальный вызов + max_iter повторных
    assert call_counter["sql_verification"] == max_iter + 1
    # Финальный output — Rejected
    assert result.output["verification_status"] == "Rejected"


def test_final_output_from_db_audit():
    """outputs.final.from_step должен указывать на последний шаг (db_audit)."""
    workflow = _load_text_to_sql_workflow()
    assert workflow.outputs["final"]["from_step"] == "db_audit"


def test_ag_ui_workflows_result_envelope_compatible():
    """Контракт AG-UI: preload_agents с правильными именами агентов должны сохраниться.
    Inputs пайплайна (max_rows, safety_level и т.д.) тоже сохраняются."""
    workflow = _load_text_to_sql_workflow()

    # Inputs сохранены
    for key in [
        "query",
        "dsn",
        "max_rows",
        "session_id",
        "run_id",
        "safety_level",
        "include_explanation",
        "validate_schema",
        "dry_run_only",
        "use_schema_suggestions",
        "allow_enhanced_fallback",
    ]:
        assert key in workflow.inputs, f"input '{key}' пропал из пайплайна"

    # Каждый агентный шаг (gen/verify/audit) обозначает свой agent_type, а не делегирует
    # его менеджеру. preload_agents больше не нужен, потому что каждый агент вызывается
    # как отдельный шаг.
    for step_id in ("sql_generation", "sql_verification", "db_audit"):
        step = next(s for s in workflow.steps if s.id == step_id)
        assert step.agent_type and step.agent_type != "manager", (
            f"{step_id} должен быть отдельным агентом, а не manager"
        )

    # Финальный шаг (db_audit) должен иметь доступ к max_rows и dry_run_only через
    # metadata (для прокидывания в БД-tool).
    audit_step = next(s for s in workflow.steps if s.id == "db_audit")
    assert audit_step.metadata.get("max_rows") == "{max_rows}"
    assert audit_step.metadata.get("dry_run_only") == "{dry_run_only}"


def test_decomposed_steps_use_substituted_metadata_via_engine():
    """Smoke-тест: новые шаги после декомпозиции корректно проходят metadata-substitution
    через engine._step_with_substituted_metadata (6.1 интеграция с 6.3)."""
    engine = _engine_instance()
    workflow = _load_text_to_sql_workflow()
    models = _workflow_models()
    WorkflowContext = models.WorkflowContext

    audit_step = next(s for s in workflow.steps if s.id == "db_audit")
    ctx = WorkflowContext(
        workflow_id="wf-1",
        session_id="sess-1",
        variables={
            "max_rows": 100,
            "dry_run_only": False,
            "safety_level": "strict",
            "include_explanation": True,
            "validate_schema": True,
            "use_schema_suggestions": True,
            "allow_enhanced_fallback": False,
            "dsn": "sqlite:///x.db",
            "run_id": "run-xyz",
        },
    )
    new_step = engine._step_with_substituted_metadata(audit_step, ctx)
    assert new_step.metadata.get("max_rows") == 100
    assert new_step.metadata.get("dry_run_only") is False
    assert new_step.metadata.get("dsn") == "sqlite:///x.db"


# ===========================================================================
# 7.22: allow_enhanced_fallback через coerce_strict_bool в EnhancedWorkflowEngine
# ===========================================================================
def _make_fallback_stub_engine():
    """Создаёт минимальный stub `_should_fallback_to_legacy` без heavy enhanced deps.

    Импорт `workflow.enhanced_engine` тянет тяжёлые зависимости (PolicyEngine,
    AdaptiveRetryEngine, CircuitBreakerManager …), которые в этом lightweight
    test-окружении не нужны. Подгружаем сам метод через ``__func__`` от исходного
    модуля и привязываем к stub-инстансу с feature_manager-стабом.
    """
    import importlib.util
    import types
    from types import SimpleNamespace

    spec = importlib.util.spec_from_file_location(
        "workflow.enhanced_engine_for_test",
        ROOT / "workflow" / "enhanced_engine.py",
    )
    # Чтение и анализ исходника без exec — нам нужен только текст метода.
    source = spec.origin
    src_text = open(source, "r", encoding="utf-8").read()  # noqa: SIM115
    # Извлекаем функции _should_fallback_to_legacy + _coerce_bool + _is_text_to_sql_workflow
    # — компилируем их изолированно.
    import ast

    tree = ast.parse(src_text)
    funcs = {}
    class_node = next(
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "EnhancedWorkflowEngine"
    )
    for n in class_node.body:
        if isinstance(n, ast.FunctionDef) and n.name in (
            "_should_fallback_to_legacy",
            "_coerce_bool",
            "_is_text_to_sql_workflow",
        ):
            mod = ast.Module(body=[n], type_ignores=[])
            code = compile(mod, source, "exec")
            ns: dict = {}
            # Inject minimal __builtins__ + needed names
            exec(  # noqa: S102
                code,
                {
                    "__builtins__": __builtins__,
                    "Any": object,
                    "Optional": object,
                    "WorkflowDefinition": object,
                    "WorkflowContext": object,
                },
                ns,
            )
            funcs[n.name] = ns[n.name]

    stub = SimpleNamespace()
    stub._coerce_bool = types.MethodType(funcs["_coerce_bool"], stub)
    stub._is_text_to_sql_workflow = types.MethodType(funcs["_is_text_to_sql_workflow"], stub)
    stub._should_fallback_to_legacy = types.MethodType(funcs["_should_fallback_to_legacy"], stub)
    stub.feature_manager = SimpleNamespace(
        workflow_overrides={"text_to_sql": {"fallback_to_legacy": False}},
        global_config={},
    )
    return stub


def _make_t2s_workflow_def():
    from types import SimpleNamespace
    return SimpleNamespace(name="text_to_sql_pipeline", metadata={})


def _make_ctx(variables: dict):
    from types import SimpleNamespace
    return SimpleNamespace(variables=variables)


def test_allow_enhanced_fallback_strict_accepts_true_values():
    stub = _make_fallback_stub_engine()
    wf = _make_t2s_workflow_def()
    for value in [True, 1, "1", "true", "TRUE", "yes", "on"]:
        ctx = _make_ctx({"allow_enhanced_fallback": value})
        assert stub._should_fallback_to_legacy(wf, ctx) is True, (
            f"value={value!r} должно coerce-иться в True"
        )


def test_allow_enhanced_fallback_strict_accepts_false_values():
    stub = _make_fallback_stub_engine()
    wf = _make_t2s_workflow_def()
    for value in [False, 0, "0", "false", "no", "off", ""]:
        ctx = _make_ctx({"allow_enhanced_fallback": value})
        assert stub._should_fallback_to_legacy(wf, ctx) is False, (
            f"value={value!r} должно coerce-иться в False"
        )


def test_allow_enhanced_fallback_strict_none_uses_default():
    stub = _make_fallback_stub_engine()
    wf = _make_t2s_workflow_def()
    # default_enabled = override.fallback_to_legacy = False
    ctx = _make_ctx({})
    assert stub._should_fallback_to_legacy(wf, ctx) is False

    # override = True → default берётся True
    stub.feature_manager.workflow_overrides = {"text_to_sql": {"fallback_to_legacy": True}}
    assert stub._should_fallback_to_legacy(wf, ctx) is True


def test_allow_enhanced_fallback_strict_rejects_invalid():
    stub = _make_fallback_stub_engine()
    wf = _make_t2s_workflow_def()
    for bad in ["maybe", "yesnt", "2", 2, -1, 1.5, [], {}]:
        ctx = _make_ctx({"allow_enhanced_fallback": bad})
        with pytest.raises(ValueError, match="allow_enhanced_fallback"):
            stub._should_fallback_to_legacy(wf, ctx)


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
