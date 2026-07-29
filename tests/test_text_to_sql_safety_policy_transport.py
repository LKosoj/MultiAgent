from __future__ import annotations

import asyncio
import sys
import types
from datetime import datetime
from pathlib import Path

import pytest
import yaml

from custom_tools.text_to_sql.validators import (
    TextToSqlSafetyPolicy,
    resolve_safety_policy,
)
from custom_tools.text_to_sql.validators import safety_config
from test_text_to_sql_agui_workflow_contract import (
    _WorkflowManagerStub,
    _load_service_with_stubs,
)


@pytest.fixture(autouse=True)
def _reset_policy_config(monkeypatch):
    monkeypatch.delenv("TEXT_TO_SQL_SAFETY_CONFIG_PATH", raising=False)
    safety_config.reset_cache()
    yield
    safety_config.reset_cache()


def test_runtime_policy_accessor_accepts_only_rehydrated_policy():
    from tool_runtime_context import (
        get_runtime_context_safety_policy,
        reset_tool_runtime_context,
        set_tool_runtime_context,
    )

    policy = resolve_safety_policy("strict")
    token = set_tool_runtime_context({"safety_policy": policy})
    try:
        assert get_runtime_context_safety_policy() is policy
    finally:
        reset_tool_runtime_context(token)

    token = set_tool_runtime_context({"safety_policy": policy.to_mapping()})
    try:
        with pytest.raises(TypeError, match="TextToSqlSafetyPolicy"):
            get_runtime_context_safety_policy()
    finally:
        reset_tool_runtime_context(token)

    assert get_runtime_context_safety_policy() is None


def test_worker_rehydrates_policy_mapping_once(monkeypatch):
    import workflow.streamlit_api as streamlit_api

    policy = resolve_safety_policy("strict")
    mapping = policy.to_mapping()
    calls = []
    captured = {}
    real_from_mapping = TextToSqlSafetyPolicy.from_mapping

    def counted_from_mapping(cls, value):
        calls.append(value)
        return real_from_mapping(value)

    class Manager:
        def __init__(self, *, use_enhanced):
            captured["use_enhanced"] = use_enhanced

        def _run_workflow_thread(
            self,
            run_id,
            workflow_path,
            parameters,
            session_id,
            client_id,
            **kwargs,
        ):
            captured["parameters"] = parameters
            captured["policy"] = parameters["safety_policy"]
            captured["supervisor_id"] = kwargs["supervisor_id"]
            captured["attempt_generation"] = kwargs["attempt_generation"]

    telemetry = types.ModuleType("telemetry")
    telemetry.get_telemetry_manager = lambda **kwargs: object()
    monkeypatch.setitem(sys.modules, "telemetry", telemetry)
    monkeypatch.setattr(
        TextToSqlSafetyPolicy,
        "from_mapping",
        classmethod(counted_from_mapping),
    )
    monkeypatch.setattr(streamlit_api, "WorkflowManager", Manager)
    monkeypatch.setattr(streamlit_api.signal, "signal", lambda *args: None)
    monkeypatch.setattr(streamlit_api.os, "setsid", lambda: None)
    monkeypatch.setattr(
        streamlit_api,
        "_setup_comprehensive_logging_from_env",
        lambda: None,
    )
    monkeypatch.setattr(streamlit_api, "_setup_process_run_log_capture", lambda run_id: None)
    monkeypatch.setenv("RUN_ID", "parent-run")

    deadline_at_ms = 9_999_999_999_999
    parameters = {"query": "show one", "safety_policy": mapping}
    streamlit_api._workflow_supervisor_process_entry(
        "run-1",
        {
            "spec_version": 1,
            "workflow_path": str(
                Path("workflow_pipelines/text_to_sql_pipeline.yaml").resolve()
            ),
            "parameters": parameters,
            "session_id": "session-1",
            "client_id": "client-1",
            "use_enhanced": True,
            "enable_telemetry": False,
            "run_incarnation": "inc-1",
            "deadline_at_ms": deadline_at_ms,
        },
        {
            "supervisor_id": "test-supervisor",
            "attempt_generation": 1,
        },
    )

    assert calls == [mapping]
    assert captured["parameters"]["safety_policy"] is captured["policy"]
    assert captured["policy"] == policy
    assert isinstance(captured["policy"], TextToSqlSafetyPolicy)
    assert captured["supervisor_id"] == "test-supervisor"
    assert captured["attempt_generation"] == 1


def test_checkpoint_round_trip_preserves_rehydrated_policy(tmp_path):
    from workflow.models import (
        WorkflowCheckpoint,
        WorkflowContext,
        WorkflowStatus,
    )
    from workflow.state_manager import SQLiteWorkflowStore

    policy = resolve_safety_policy("strict")
    store = SQLiteWorkflowStore(str(tmp_path / "workflow.db"))
    checkpoint = WorkflowCheckpoint(
        workflow_id="workflow-policy-round-trip",
        timestamp=datetime.now(),
        status=WorkflowStatus.RUNNING,
        context=WorkflowContext(
            workflow_id="workflow-policy-round-trip",
            session_id="session-1",
            variables={"safety_policy": policy},
        ),
    )

    asyncio.run(store.save_checkpoint(checkpoint))
    restored = asyncio.run(
        store.get_latest_checkpoint("workflow-policy-round-trip")
    )

    assert restored is not None
    assert restored.context is not None
    restored_policy = restored.context.variables["safety_policy"]
    assert isinstance(restored_policy, TextToSqlSafetyPolicy)
    assert restored_policy == policy


def test_pipeline_carries_one_policy_object_through_mandatory_stages():
    pipeline = yaml.safe_load(
        (Path(__file__).parents[1] / "workflow_pipelines/text_to_sql_pipeline.yaml").read_text(
            encoding="utf-8"
        )
    )
    steps = {step["id"]: step for step in pipeline["steps"]}
    placeholder = "{safety_policy}"

    assert pipeline["inputs"]["safety_policy"] is None
    assert steps["sql_generation"]["metadata"]["safety_policy"] == placeholder
    assert steps["sql_verification"]["metadata"]["safety_policy"] == placeholder
    assert steps["db_audit"]["metadata"]["safety_policy"] == placeholder
    assert steps["db_audit"]["tool_params"]["safety_policy"] == placeholder


def test_workflow_env_does_not_mutate_safety_level(monkeypatch):
    import workflow.streamlit_api as streamlit_api

    monkeypatch.setenv("TEXT_TO_SQL_SAFETY_LEVEL", "operator-value")

    with streamlit_api._workflow_dsn_env(
        {"safety_level": "strict"},
        workflow_name="text_to_sql_pipeline",
    ):
        assert streamlit_api.os.environ["TEXT_TO_SQL_SAFETY_LEVEL"] == "operator-value"

    assert streamlit_api.os.environ["TEXT_TO_SQL_SAFETY_LEVEL"] == "operator-value"


def test_service_resolves_and_transports_private_policy_before_start(monkeypatch):
    manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, manager)
    policy = resolve_safety_policy("strict")
    levels = []

    def resolve(level):
        levels.append(level)
        return policy

    monkeypatch.setattr(service, "resolve_safety_policy", resolve)

    result = service.handle_service_action(
        "presets.text_to_sql.generate",
        {
            "query": "show one",
            "dsn": "sqlite:///tmp/transport.db",
            "admin_raw_dsn_compat": True,
        },
    )

    assert levels == ["strict"]
    assert manager.calls[0]["parameters"]["safety_policy"] == policy.to_mapping()
    assert "safety_policy" not in result["parameters"]
    assert result["parameters"]["safety_level"] == "strict"


def test_policy_resolution_failure_prevents_worker_start(monkeypatch):
    manager = _WorkflowManagerStub()
    service = _load_service_with_stubs(monkeypatch, manager)

    def fail_resolution(level):
        raise ValueError("malformed safety policy")

    monkeypatch.setattr(service, "resolve_safety_policy", fail_resolution)

    with pytest.raises(ValueError, match="malformed safety policy"):
        service.handle_service_action(
            "presets.text_to_sql.generate",
            {
                "query": "show one",
                "dsn": "sqlite:///tmp/transport.db",
                "admin_raw_dsn_compat": True,
            },
        )

    assert manager.calls == []
