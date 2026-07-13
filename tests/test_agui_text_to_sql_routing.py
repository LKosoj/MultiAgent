from __future__ import annotations

import sys
import types

import pytest

from backend.fastapi_app.agui.events import EventType
from backend.fastapi_app.agui.models import RunAgentInput


@pytest.mark.asyncio
async def test_generic_text_to_sql_intent_requires_service_action_without_db_dsn(
    monkeypatch,
) -> None:
    import agent_system

    class _Response:
        content = "text_to_sql"

    monkeypatch.setattr(agent_system, "model_summary", lambda _messages: _Response())
    original_getenv = agent_system.os.getenv

    def _guarded_getenv(key: str, default=None):
        if key == "DB_DSN":
            raise AssertionError("generic routing must not read DB_DSN")
        return original_getenv(key, default)

    monkeypatch.setattr(agent_system.os, "getenv", _guarded_getenv)
    system = agent_system.DynamicAgentSystem.__new__(agent_system.DynamicAgentSystem)

    with pytest.raises(Exception) as exc_info:
        await system.analyze_task("show monthly sales")

    assert exc_info.type.__name__ == "TextToSqlServiceActionRequiredError"
    assert (
        getattr(exc_info.value, "error_code", None)
        == "text_to_sql_service_action_required"
    )


@pytest.mark.asyncio
async def test_runner_exposes_stable_generic_text_to_sql_handoff_code(
    monkeypatch,
) -> None:
    from backend.fastapi_app.agui import runner

    class _HandoffError(ValueError):
        error_code = "text_to_sql_service_action_required"

    class _DynamicAgentSystem:
        async def coordinate(self, **_kwargs):
            raise _HandoffError("use presets.text_to_sql.generate")

    module = types.ModuleType("agent_system")
    module.DynamicAgentSystem = _DynamicAgentSystem
    monkeypatch.setitem(sys.modules, "agent_system", module)
    monkeypatch.setattr(runner, "get_logging_manager", lambda *_args: None)

    input_data = RunAgentInput(
        **{
            "threadId": "thread-1",
            "runId": "run-1",
            "state": {},
            "messages": [
                {
                    "id": "message-1",
                    "role": "user",
                    "content": "show monthly sales",
                }
            ],
            "tools": [],
            "context": [],
            "forwardedProps": {},
        }
    )

    events = [event async for event in runner.run_agent(input_data)]
    error = next(event for event in events if event.type == EventType.RUN_ERROR)

    assert error.code == "text_to_sql_service_action_required"

