"""Pin-тесты Group C1 (Wave1): json-output schema для шагов workflow.

Контракт:
- ``WorkflowStep.output_schema`` загружается из YAML через
  ``WorkflowDefinition.from_dict`` и сохраняется обратно через ``to_dict``.
- Метод ``WorkflowEngine._normalize_step_output(step, raw)`` парсит str-output
  как JSON, если ``step.output_schema == "json_object"``; bare str без схемы
  возвращается без изменений; невалидный JSON при объявленной схеме —
  ``WorkflowStepError`` с redacted-фрагментом первых 200 символов raw.
- Интеграция в ``_on_step_completed``: после парсинга dict сохраняется
  в ``context.step_outputs[step.id]`` и dotted-ключи (``step.field``)
  становятся доступны для ``_evaluate_condition``.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import pytest

# Для импорта core text_to_sql косвенно — у других тестов в сьюте это требуется,
# здесь мы только грузим workflow.models/engine, а env-setup (safety profile)
# выполнен в conftest.py.


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


@pytest.fixture
def workflow_pkg():
    return _install_light_workflow_package()


@pytest.fixture
def engine(workflow_pkg):
    return object.__new__(workflow_pkg.engine.WorkflowEngine)


def _make_step(workflow_pkg, **overrides):
    models = workflow_pkg.models
    kwargs = dict(id="verifier", task="check", output_schema=None)
    kwargs.update(overrides)
    return models.WorkflowStep(**kwargs)


# ---------------------------------------------------------------------------
# WorkflowStep.output_schema через WorkflowDefinition
# ---------------------------------------------------------------------------
def test_workflow_definition_loads_output_schema_from_dict(workflow_pkg):
    data = {
        "name": "pipe",
        "steps": [
            {
                "id": "verify",
                "task": "ok",
                "agent_type": "sql_verifier_agent",
                "output_schema": "json_object",
                "output_schema_requirements": {
                    "required": ["verification_status"],
                    "properties": {"verification_status": {"enum": ["Approved", "Rejected"]}},
                },
            }
        ],
    }
    wf = workflow_pkg.models.WorkflowDefinition.from_dict(data)
    assert wf.steps[0].output_schema == "json_object"
    assert wf.steps[0].output_schema_requirements["required"] == ["verification_status"]


def test_workflow_definition_rejects_unknown_output_schema(workflow_pkg):
    data = {
        "name": "pipe",
        "steps": [
            {
                "id": "verify",
                "task": "ok",
                "output_schema": "jsonobject",
            }
        ],
    }
    with pytest.raises(ValueError, match="Unsupported output_schema"):
        workflow_pkg.models.WorkflowDefinition.from_dict(data)


def test_workflow_definition_roundtrip_preserves_output_schema(workflow_pkg):
    data = {
        "name": "pipe",
        "steps": [
            {
                "id": "verify",
                "task": "ok",
                "agent_type": "sql_verifier_agent",
                "output_schema": "json_object",
                "output_schema_requirements": {
                    "required": ["verification_status"],
                    "properties": {"verification_status": {"enum": ["Approved", "Rejected"]}},
                },
            }
        ],
    }
    wf = workflow_pkg.models.WorkflowDefinition.from_dict(data)
    serialized = wf.to_dict()
    assert serialized["steps"][0]["output_schema"] == "json_object"
    assert serialized["steps"][0]["output_schema_requirements"]["required"] == ["verification_status"]


def test_workflow_definition_coerces_requires_enhanced_engine_false_string(workflow_pkg):
    wf = workflow_pkg.models.WorkflowDefinition.from_dict({
        "name": "pipe",
        "pipeline": {"requires_enhanced_engine": "false"},
        "steps": [],
    })

    assert wf.requires_enhanced_engine is False
    assert "pipeline" not in wf.to_dict()


def test_workflow_definition_rejects_invalid_requires_enhanced_engine(workflow_pkg):
    with pytest.raises(ValueError, match="pipeline.requires_enhanced_engine must be boolean"):
        workflow_pkg.models.WorkflowDefinition.from_dict({
            "name": "pipe",
            "pipeline": {"requires_enhanced_engine": "sometimes"},
            "steps": [],
        })


# ---------------------------------------------------------------------------
# _normalize_step_output
# ---------------------------------------------------------------------------
def test_normalize_step_output_parses_json_string_when_schema_declared(engine, workflow_pkg):
    step = _make_step(workflow_pkg, output_schema="json_object")
    normalized, parsed = engine._normalize_step_output(
        step, '{"verification_status": "Rejected", "n": 3}'
    )
    assert parsed is True
    assert normalized == {"verification_status": "Rejected", "n": 3}


def test_normalize_step_output_raises_on_invalid_json_when_schema_declared(engine, workflow_pkg):
    step = _make_step(workflow_pkg, output_schema="json_object")
    with pytest.raises(workflow_pkg.models.WorkflowStepError) as ei:
        engine._normalize_step_output(step, "not-json-at-all")
    msg = str(ei.value)
    assert "verifier" in msg
    assert "not-json-at-all" in msg


def test_normalize_step_output_redacts_invalid_json_snippet(engine, workflow_pkg):
    step = _make_step(workflow_pkg, output_schema="json_object")
    raw = (
        "driver failed postgresql://alice:secret@db.example.com/app"
        "?api_key=rawkey not-json"
    )

    with pytest.raises(workflow_pkg.models.WorkflowStepError) as ei:
        engine._normalize_step_output(step, raw)

    msg = str(ei.value)
    assert "alice:secret" not in msg
    assert "rawkey" not in msg
    assert "postgresql://***:***@" in msg


def test_normalize_step_output_raises_on_empty_when_schema_declared(engine, workflow_pkg):
    step = _make_step(workflow_pkg, output_schema="json_object")
    with pytest.raises(workflow_pkg.models.WorkflowStepError):
        engine._normalize_step_output(step, "   ")


def test_normalize_step_output_passes_through_str_when_no_schema(engine, workflow_pkg):
    step = _make_step(workflow_pkg, output_schema=None)
    raw = "not json but no schema"
    normalized, parsed = engine._normalize_step_output(step, raw)
    assert parsed is False
    assert normalized == raw


def test_normalize_step_output_does_not_parse_json_without_schema(engine, workflow_pkg):
    step = _make_step(workflow_pkg, output_schema=None)
    raw = '{"a": 1}'

    normalized, parsed = engine._normalize_step_output(step, raw)

    assert parsed is False
    assert normalized == raw


def test_normalize_step_output_rejects_unknown_schema(engine, workflow_pkg):
    step = _make_step(workflow_pkg, output_schema="text")
    raw = '{"a": 1}'

    with pytest.raises(workflow_pkg.models.WorkflowStepError, match="unsupported output_schema"):
        engine._normalize_step_output(step, raw)


def test_normalize_step_output_passes_through_dict(engine, workflow_pkg):
    step = _make_step(workflow_pkg, output_schema="json_object")
    payload = {"k": 1}
    normalized, parsed = engine._normalize_step_output(step, payload)
    # dict не str → ничего не парсим, флаг False
    assert parsed is False
    assert normalized is payload


def test_normalize_step_output_enforces_required_fields(engine, workflow_pkg):
    step = _make_step(
        workflow_pkg,
        output_schema="json_object",
        output_schema_requirements={
            "required": [
                "verification_status",
                "safety_check",
                "performance_check",
                "recommendations",
            ],
            "properties": {
                "verification_status": {"enum": ["Approved", "Rejected"]},
            },
        },
    )

    with pytest.raises(workflow_pkg.models.WorkflowStepError, match="missing required fields"):
        engine._normalize_step_output(step, "{}")


def test_normalize_step_output_enforces_enum_fields(engine, workflow_pkg):
    step = _make_step(
        workflow_pkg,
        output_schema="json_object",
        output_schema_requirements={
            "required": ["verification_status"],
            "properties": {
                "verification_status": {"enum": ["Approved", "Rejected"]},
            },
        },
    )

    with pytest.raises(workflow_pkg.models.WorkflowStepError, match="must be one of"):
        engine._normalize_step_output(step, '{"verification_status": "Maybe"}')


@pytest.mark.parametrize("payload", [[{"k": 1}], ["x"], 1, True, None])
def test_normalize_step_output_rejects_non_dict_for_json_object(engine, workflow_pkg, payload):
    step = _make_step(workflow_pkg, output_schema="json_object")

    with pytest.raises(workflow_pkg.models.WorkflowStepError, match="expected dict"):
        engine._normalize_step_output(step, payload)


def test_normalize_step_output_rejects_unknown_schema_for_non_string(engine, workflow_pkg):
    step = _make_step(workflow_pkg, output_schema="text")

    with pytest.raises(workflow_pkg.models.WorkflowStepError, match="unsupported output_schema"):
        engine._normalize_step_output(step, {"k": 1})


def test_normalize_step_output_truncates_long_raw_in_error(engine, workflow_pkg):
    step = _make_step(workflow_pkg, output_schema="json_object")
    huge = "x" * 1000
    with pytest.raises(workflow_pkg.models.WorkflowStepError) as ei:
        engine._normalize_step_output(step, huge)
    # 200 символов raw должны попасть в сообщение, 800+ — нет
    msg = str(ei.value)
    assert "x" * 200 in msg
    # Сообщение должно быть существенно короче 1000 символов от raw payload.
    assert msg.count("x") < 250


# ---------------------------------------------------------------------------
# Интеграция: _on_step_completed парсит JSON и кладёт dotted ключи.
# ---------------------------------------------------------------------------
def test_on_step_completed_flattens_after_json_parse(workflow_pkg, monkeypatch):
    engine_module = workflow_pkg.engine
    models = workflow_pkg.models

    engine = object.__new__(engine_module.WorkflowEngine)

    class _StubStateManager:
        async def save_checkpoint(self, **kwargs):
            return None

    engine.state_manager = _StubStateManager()

    step = models.WorkflowStep(
        id="verifier",
        task="t",
        agent_type="sql_verifier_agent",
        output_schema="json_object",
    )
    raw = '{"verification_status": "Rejected", "recommendations": "fix it"}'
    step_result = models.StepResult(
        step_id="verifier",
        status=models.StepStatus.COMPLETED,
        output=raw,
    )
    context = models.WorkflowContext(workflow_id="wf-1")

    asyncio.run(
        engine._on_step_completed("wf-1", step, step_result, context, {})
    )

    # output должен быть подменён на dict
    assert isinstance(step_result.output, dict)
    assert step_result.output["verification_status"] == "Rejected"

    # context.step_outputs должен содержать и сам шаг, и dotted-keys
    assert context.step_outputs["verifier"] == {
        "verification_status": "Rejected",
        "recommendations": "fix it",
    }
    assert context.step_outputs["verifier.verification_status"] == "Rejected"
    assert context.step_outputs["verifier.recommendations"] == "fix it"


def test_parallel_step_wrapper_passes_shared_step_results_to_checkpoint(workflow_pkg):
    engine_module = workflow_pkg.engine
    models = workflow_pkg.models

    engine = object.__new__(engine_module.WorkflowEngine)
    step = models.WorkflowStep(id="current", task="t", agent_type="x")
    current_result = models.StepResult(
        step_id="current",
        status=models.StepStatus.COMPLETED,
        output={"value": 1},
    )
    previous_result = models.StepResult(
        step_id="previous",
        status=models.StepStatus.COMPLETED,
        output={"value": 0},
    )
    shared_results = {"previous": previous_result}
    context = models.WorkflowContext(workflow_id="wf-parallel")
    context._workflow_definition = models.WorkflowDefinition(name="wf-parallel", steps=[step])
    context._workflow_step_results = shared_results
    captured = {}

    async def fake_execute(step_arg, context_arg, workflow_def_arg, step_results=None):
        captured["execute_step_results"] = step_results
        return current_result

    async def fake_completed(workflow_id, step_arg, result_arg, context_arg, step_results_arg):
        captured["completed_step_results"] = step_results_arg
        return None

    engine._execute_workflow_step = fake_execute
    engine._on_step_completed = fake_completed

    result = asyncio.run(engine._execute_workflow_step_wrapper(step, context))

    assert result is current_result
    assert captured["execute_step_results"] is shared_results
    assert captured["completed_step_results"] is shared_results
    assert shared_results["previous"] is previous_result
    assert shared_results["current"] is current_result


def test_on_step_completed_preserves_non_str_output(workflow_pkg):
    engine_module = workflow_pkg.engine
    models = workflow_pkg.models

    engine = object.__new__(engine_module.WorkflowEngine)

    class _StubStateManager:
        async def save_checkpoint(self, **kwargs):
            return None

    engine.state_manager = _StubStateManager()

    step = models.WorkflowStep(
        id="step1",
        task="t",
        agent_type="x",
        output_schema=None,
    )
    payload = {"already": "dict"}
    step_result = models.StepResult(
        step_id="step1",
        status=models.StepStatus.COMPLETED,
        output=payload,
    )
    context = models.WorkflowContext(workflow_id="wf-1")

    asyncio.run(engine._on_step_completed("wf-1", step, step_result, context, {}))

    assert step_result.output is payload
    assert context.step_outputs["step1"] is payload
    assert context.step_outputs["step1.already"] == "dict"
