def test_with_telemetry_preserves_positional_session_id(monkeypatch):
    import tool_manager

    calls = []

    class FakeToolManager:
        def run_tool(self, tool_name, tool_function, task_description="", session_id=None, **kwargs):
            calls.append({"tool_name": tool_name, "session_id": session_id, "kwargs": dict(kwargs)})
            if session_id and "session_id" not in kwargs:
                kwargs["session_id"] = session_id
            return tool_function(**kwargs)

    monkeypatch.setattr(tool_manager, "get_tool_manager", lambda: FakeToolManager())

    @tool_manager.with_telemetry("demo")
    def sample_tool(session_id, value):
        return {"session_id": session_id, "value": value}

    result = sample_tool("sid-pos", 42)

    assert result == {"session_id": "sid-pos", "value": 42}
    assert calls == [{"tool_name": "demo", "session_id": "sid-pos", "kwargs": {"value": 42}}]


def test_with_telemetry_preserves_positional_only_semantics(monkeypatch):
    import tool_manager

    def fail_get_tool_manager():
        raise AssertionError("positional-only calls must bypass telemetry")

    monkeypatch.setattr(tool_manager, "get_tool_manager", fail_get_tool_manager)

    @tool_manager.with_telemetry("demo")
    def sample_tool(session_id, /, value):
        return {"session_id": session_id, "value": value}

    assert sample_tool("sid-pos", 42) == {"session_id": "sid-pos", "value": 42}


def test_with_telemetry_preserves_duplicate_argument_error(monkeypatch):
    import pytest
    import tool_manager

    def fail_get_tool_manager():
        raise AssertionError("invalid calls must bypass telemetry")

    monkeypatch.setattr(tool_manager, "get_tool_manager", fail_get_tool_manager)

    @tool_manager.with_telemetry("demo")
    def sample_tool(session_id, value):
        return {"session_id": session_id, "value": value}

    with pytest.raises(TypeError):
        sample_tool("sid-pos", session_id="sid-kw", value=42)
