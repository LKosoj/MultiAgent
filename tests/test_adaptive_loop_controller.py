"""Deterministic contracts for the durable serial adaptive tool loop."""

from __future__ import annotations

from collections.abc import Mapping
import gc
import threading
import types

import pytest

from custom_tools.text_to_sql.adaptive.controller import (
    AdaptiveLoopActionError,
    AdaptiveLoopCancelled,
    AdaptiveLoopController,
    AdaptiveLoopIndeterminateError,
    AdaptiveLoopResult,
    AdaptiveLoopStepLimitError,
    AdaptiveLoopTerminalError,
    AdaptiveLoopToolFailure,
    AdaptiveLoopUnknownToolError,
    CallableToolAdapter,
    AdaptiveLoopUnsupportedAgentError,
    MappingToolResolver,
    MAX_MODEL_ACTION_BYTES,
    NormalizedToolResult,
    ToolManagerAdapter,
    ToolInvocation,
    parse_model_action,
    require_tool_calling_agent,
)
from custom_tools.text_to_sql.adaptive.serialization import canonical_json_bytes
from workflow.adaptive_state_store import (
    AdaptiveActionPhase,
    AdaptiveCheckpointKey,
    AdaptiveLoopKind,
    AdaptiveStateStore,
)
from workflow.deadline import DeadlineBudget, WorkflowDeadlineExceeded


class _Model:
    def __init__(
        self, actions: list[Mapping[str, object]], events: list[object]
    ) -> None:
        self._actions = list(actions)
        self.events = events
        self.histories: list[tuple[object, ...]] = []

    def next_action(self, history):
        self.events.append(("model", len(history)))
        self.histories.append(history)
        return self._actions.pop(0)


class _Adapter:
    def __init__(
        self, events: list[object], result=None, error: Exception | None = None
    ) -> None:
        self.events = events
        self.result = result or NormalizedToolResult("success", {"ok": True})
        self.error = error
        self.calls: list[ToolInvocation] = []

    def execute(self, invocation: ToolInvocation) -> NormalizedToolResult:
        self.calls.append(invocation)
        self.events.append(("tool", invocation.tool_call.tool_call_id))
        if self.error is not None:
            raise self.error
        return self.result


class _RecordingStore:
    def __init__(self, store: AdaptiveStateStore, events: list[object]) -> None:
        self._store = store
        self.events = events

    def get_snapshot(self, key):
        return self._store.get_snapshot(key)

    def on_state_transition(self, key, phase, **kwargs):
        result = self._store.on_state_transition(key, phase, **kwargs)
        self.events.append(("commit", phase.value, key.revision))
        return result


def _tool(tool_call_id: str, tool_name: str = "inspect", **arguments):
    return {
        "kind": "tool",
        "tool_call_id": tool_call_id,
        "tool_name": tool_name,
        "arguments": arguments,
    }


def _final(answer="done"):
    return {"kind": "final", "answer": answer}


def _awaitable_with_closed_probe(kind: str):
    if kind == "coroutine":

        async def delayed():
            return {"ok": True}

        value = delayed()
        return value, lambda: value.cr_frame is None
    if kind == "generator":

        @types.coroutine
        def delayed():
            yield None
            return {"ok": True}

        value = delayed()
        return value, lambda: value.gi_frame is None

    class _CustomAwaitable:
        def __init__(self) -> None:
            self.closed = False

        def __await__(self):
            if False:
                yield None
            return {"ok": True}

        def close(self) -> None:
            self.closed = True

    value = _CustomAwaitable()
    return value, lambda: value.closed


def _controller(
    tmp_path,
    model,
    adapter,
    events,
    *,
    store=None,
    **kwargs,
):
    raw_store = store or AdaptiveStateStore(tmp_path / "state.db")
    return AdaptiveLoopController(
        model=model,
        tools=MappingToolResolver({"inspect": adapter}),
        state_store=_RecordingStore(raw_store, events),
        run_id="run-1",
        run_incarnation="inc-1",
        loop_kind=AdaptiveLoopKind.SOLVER,
        **kwargs,
    ), raw_store


def _key(revision: int = 0) -> AdaptiveCheckpointKey:
    return AdaptiveCheckpointKey("run-1", "inc-1", AdaptiveLoopKind.SOLVER, revision)


def test_each_accepted_tool_result_is_committed_before_next_model_turn(
    tmp_path,
) -> None:
    events: list[object] = []
    model = _Model(
        [_tool("call-1", query="a"), _tool("call-2", query="b"), _final()], events
    )
    adapter = _Adapter(events)
    controller, store = _controller(tmp_path, model, adapter, events)

    result = controller.run()

    assert result.answer == "done"
    assert [message.tool_call_id for message in result.accepted_results] == [
        "call-1",
        "call-2",
    ]
    assert events == [
        ("model", 0),
        ("commit", "planned", 0),
        ("tool", "call-1"),
        ("commit", "observed", 0),
        ("model", 1),
        ("commit", "planned", 1),
        ("tool", "call-2"),
        ("commit", "observed", 1),
        ("model", 2),
        ("commit", "terminal", 2),
    ]
    assert model.histories[1][0].result.value == {"ok": True}
    assert store.get_snapshot(_key()).observed is not None
    assert store.get_snapshot(_key(1)).observed is not None


def test_normalized_recoverable_tool_error_is_an_accepted_result(tmp_path) -> None:
    events: list[object] = []
    model = _Model([_tool("call-1"), _final()], events)
    adapter = _Adapter(
        events, result=NormalizedToolResult("error", {"reason": "retry"})
    )
    controller, store = _controller(tmp_path, model, adapter, events)

    result = controller.run()

    assert result.accepted_results[0].result.status == "error"
    assert store.get_snapshot(_key()).observed.action["status"] == "error"


@pytest.mark.parametrize(
    "action",
    [
        {"kind": "tool", "tool_call_id": "call-1", "tool_name": "inspect"},
        {
            "kind": "tool",
            "tool_call_id": "call-1",
            "tool_name": "inspect",
            "arguments": {},
            "extra": 1,
        },
        {
            "kind": "tool",
            "tool_call_id": "call-1",
            "tool_name": "inspect",
            "arguments": {1: "bad"},
        },
    ],
)
def test_malformed_action_is_rejected_before_any_durable_transition(
    tmp_path, action
) -> None:
    events: list[object] = []
    model = _Model([action], events)
    adapter = _Adapter(events)
    controller, store = _controller(tmp_path, model, adapter, events)

    with pytest.raises(AdaptiveLoopActionError):
        controller.run()

    assert adapter.calls == []
    assert store.get_snapshot(_key()).planned is None


def test_model_action_canonical_byte_bound_is_exact() -> None:
    boundary = _tool("call-1", payload="")
    boundary["arguments"]["payload"] = "x" * (
        MAX_MODEL_ACTION_BYTES - len(canonical_json_bytes(boundary))
    )

    assert len(canonical_json_bytes(boundary)) == MAX_MODEL_ACTION_BYTES
    assert (
        parse_model_action(boundary).arguments["payload"]
        == boundary["arguments"]["payload"]
    )
    boundary["arguments"]["payload"] += "x"
    with pytest.raises(AdaptiveLoopActionError, match="byte bound"):
        parse_model_action(boundary)


def test_oversized_model_action_is_rejected_before_planned_persistence(
    tmp_path,
) -> None:
    events: list[object] = []
    oversized = _tool("call-1", payload="x" * MAX_MODEL_ACTION_BYTES)
    adapter = _Adapter(events)
    controller, store = _controller(
        tmp_path,
        _Model([oversized], events),
        adapter,
        events,
    )

    with pytest.raises(AdaptiveLoopActionError, match="byte bound"):
        controller.run()

    assert adapter.calls == []
    assert store.get_snapshot(_key()).planned is None


def test_unknown_tool_is_rejected_before_planning(tmp_path) -> None:
    events: list[object] = []
    model = _Model([_tool("call-1", tool_name="unknown")], events)
    adapter = _Adapter(events)
    controller, store = _controller(tmp_path, model, adapter, events)

    with pytest.raises(AdaptiveLoopUnknownToolError):
        controller.run()

    assert store.get_snapshot(_key()).planned is None


def test_duplicate_model_tool_call_reuses_committed_result_without_second_callback(
    tmp_path,
) -> None:
    events: list[object] = []
    model = _Model(
        [_tool("call-1", query="same"), _tool("call-1", query="same"), _final()], events
    )
    adapter = _Adapter(events)
    controller, store = _controller(tmp_path, model, adapter, events)

    result = controller.run()

    assert len(adapter.calls) == 1
    assert [event for event in events if event[:2] == ("commit", "observed")] == [
        ("commit", "observed", 0)
    ]
    assert len(result.accepted_results) == 1
    assert store.get_snapshot(_key(1)).planned is None


def test_duplicate_identity_with_different_arguments_fails_closed(tmp_path) -> None:
    events: list[object] = []
    model = _Model(
        [_tool("call-1", query="first"), _tool("call-1", query="other")], events
    )
    adapter = _Adapter(events)
    controller, _ = _controller(tmp_path, model, adapter, events)

    with pytest.raises(AdaptiveLoopActionError, match="different call semantics"):
        controller.run()

    assert len(adapter.calls) == 1


@pytest.mark.parametrize(
    ("first_value", "duplicate_value"),
    [(1, True), (0, False), (1, 1.0)],
)
def test_duplicate_identity_uses_canonical_json_type_equality(
    tmp_path,
    first_value,
    duplicate_value,
) -> None:
    events: list[object] = []
    model = _Model(
        [
            _tool("call-1", value=first_value),
            _tool("call-1", value=duplicate_value),
        ],
        events,
    )
    adapter = _Adapter(events)
    controller, _ = _controller(tmp_path, model, adapter, events)

    with pytest.raises(AdaptiveLoopActionError, match="different call semantics"):
        controller.run()

    assert len(adapter.calls) == 1


def test_repeated_exact_duplicate_is_bounded_by_model_turn_budget(tmp_path) -> None:
    events: list[object] = []
    model = _Model([_tool("call-1"), _tool("call-1"), _tool("call-1")], events)
    adapter = _Adapter(events)
    controller, store = _controller(
        tmp_path,
        model,
        adapter,
        events,
        max_model_turns=3,
    )

    with pytest.raises(AdaptiveLoopStepLimitError, match="max_model_turns"):
        controller.run()

    assert len(adapter.calls) == 1
    assert store.get_snapshot(_key()).observed is not None
    assert store.get_snapshot(_key(1)).planned is None


def test_external_consumers_cannot_mutate_internal_arguments_or_result(
    tmp_path,
) -> None:
    events: list[object] = []

    class _MutatingModel(_Model):
        def next_action(self, history):
            if history:
                history[0].arguments["nested"]["value"] = "model changed"
                history[0].result.value["nested"]["value"] = "model changed"
            return super().next_action(history)

    class _MutatingAdapter(_Adapter):
        def execute(self, invocation):
            invocation.tool_call.arguments["nested"]["value"] = "tool changed"
            return super().execute(invocation)

    def mutate_callback(invocation, result) -> None:
        invocation.tool_call.arguments["nested"]["value"] = "callback changed"
        result.value["nested"]["value"] = "callback changed"

    model = _MutatingModel(
        [_tool("call-1", nested={"value": "original"}), _final()], events
    )
    adapter = _MutatingAdapter(
        events,
        result=NormalizedToolResult("success", {"nested": {"value": "original"}}),
    )
    controller, store = _controller(
        tmp_path,
        model,
        adapter,
        events,
        before_observed=mutate_callback,
        after_observed=mutate_callback,
    )

    result = controller.run()

    assert result.accepted_results[0].arguments == {"nested": {"value": "original"}}
    assert result.accepted_results[0].result.value == {"nested": {"value": "original"}}
    snapshot = store.get_snapshot(_key())
    assert snapshot.planned.action["arguments"] == {"nested": {"value": "original"}}
    assert snapshot.observed.action["value"] == {"nested": {"value": "original"}}


def test_tool_exception_and_timeout_leave_only_planned_call_and_resume_fails_closed(
    tmp_path,
) -> None:
    events: list[object] = []
    model = _Model([_tool("call-1")], events)
    adapter = _Adapter(events, error=TimeoutError("slow"))
    controller, store = _controller(tmp_path, model, adapter, events)

    with pytest.raises(AdaptiveLoopToolFailure) as error:
        controller.run()
    assert isinstance(error.value.__cause__, TimeoutError)
    snapshot = store.get_snapshot(_key())
    assert snapshot.planned is not None
    assert snapshot.observed is None

    resumed_events: list[object] = []
    resumed_model = _Model([_final()], resumed_events)
    resumed, _ = _controller(
        tmp_path, resumed_model, _Adapter(resumed_events), resumed_events, store=store
    )
    with pytest.raises(
        AdaptiveLoopIndeterminateError, match="planned without a durable result"
    ):
        resumed.run()
    assert resumed_model.histories == []


def test_cancel_after_tool_result_before_acceptance_has_no_observed_callback(
    tmp_path,
) -> None:
    events: list[object] = []
    cancelled = {"value": False}
    model = _Model([_tool("call-1")], events)
    adapter = _Adapter(events)

    def cancel_before_observed(*_args) -> None:
        cancelled["value"] = True

    controller, store = _controller(
        tmp_path,
        model,
        adapter,
        events,
        is_cancelled=lambda: cancelled["value"],
        before_observed=cancel_before_observed,
    )

    with pytest.raises(AdaptiveLoopCancelled):
        controller.run()
    assert store.get_snapshot(_key()).planned is not None
    assert store.get_snapshot(_key()).observed is None


def test_absolute_deadline_is_passed_to_adapter_and_checked_before_acceptance(
    tmp_path,
) -> None:
    events: list[object] = []
    clock = {"now": 10.0}
    deadline = DeadlineBudget(
        deadline_monotonic=20.0,
        deadline_at_ms=20_000,
        monotonic=lambda: clock["now"],
        wall_time=lambda: 0.0,
    )
    model = _Model([_tool("call-1")], events)

    class _ExpiresAdapter(_Adapter):
        def execute(self, invocation):
            result = super().execute(invocation)
            clock["now"] = 20.0
            return result

    adapter = _ExpiresAdapter(events)
    controller, store = _controller(tmp_path, model, adapter, events, deadline=deadline)

    with pytest.raises(WorkflowDeadlineExceeded):
        controller.run()
    assert adapter.calls[0].remaining_seconds == 10.0
    assert store.get_snapshot(_key()).planned is not None
    assert store.get_snapshot(_key()).observed is None


def test_tool_manager_adapter_forwards_existing_deadline_parameter(tmp_path) -> None:
    events: list[object] = []
    clock = {"now": 10.0}
    deadline = DeadlineBudget(
        deadline_monotonic=20.0,
        deadline_at_ms=20_000,
        monotonic=lambda: clock["now"],
        wall_time=lambda: 0.0,
    )

    class _ToolManager:
        def __init__(self) -> None:
            self.invocation_id = None

        def run_tool(self, *, tool_function, tool_invocation_id, **kwargs):
            self.invocation_id = tool_invocation_id
            return tool_function(query=kwargs["query"], deadline=kwargs["deadline"])

    manager = _ToolManager()

    def inspect(*, query, deadline):
        assert deadline is not None
        assert deadline.remaining_seconds() == 10.0
        return {"query": query}

    adapter = ToolManagerAdapter(
        tool_manager=manager,
        tool=inspect,
        task_description="inspect query",
    )
    model = _Model([_tool("call-1", query="orders"), _final()], events)
    raw_store = AdaptiveStateStore(tmp_path / "tool-manager.db")
    controller = AdaptiveLoopController(
        model=model,
        tools=MappingToolResolver({"inspect": adapter}),
        state_store=_RecordingStore(raw_store, events),
        run_id="run-1",
        run_incarnation="inc-1",
        loop_kind=AdaptiveLoopKind.SOLVER,
        deadline=deadline,
    )

    result = controller.run()

    assert result.accepted_results[0].result.value == {"query": "orders"}
    assert manager.invocation_id.startswith("invoke:")


def test_tool_manager_adapter_rejects_async_callable_without_creating_coroutine() -> (
    None
):
    class _ToolManager:
        def run_tool(self, **_kwargs):
            raise AssertionError("async tool must be rejected before run_tool")

    async def inspect(*, query):
        return {"query": query}

    with pytest.raises(TypeError, match="async tools"):
        ToolManagerAdapter(
            tool_manager=_ToolManager(),
            tool=inspect,
            task_description="inspect query",
        )


@pytest.mark.parametrize("adapter_kind", ["callable", "tool_manager"])
def test_disguised_async_tool_result_is_closed_and_never_observed(
    tmp_path, adapter_kind
) -> None:
    events: list[object] = []

    async def delayed_result():
        return {"ok": True}

    def inspect(**_kwargs):
        return delayed_result()

    if adapter_kind == "callable":
        adapter = CallableToolAdapter(inspect)
    else:

        class _ToolManager:
            def run_tool(self, *, tool_function, **kwargs):
                return tool_function(**kwargs)

        adapter = ToolManagerAdapter(
            tool_manager=_ToolManager(),
            tool=inspect,
            task_description="inspect query",
        )

    controller, store = _controller(
        tmp_path,
        _Model([_tool("call-1")], events),
        adapter,
        events,
    )

    with pytest.raises(
        AdaptiveLoopToolFailure, match="failed before acceptance"
    ) as error:
        controller.run()
    assert isinstance(error.value.__cause__, TypeError)
    gc.collect()
    assert store.get_snapshot(_key()).observed is None


@pytest.mark.parametrize("awaitable_kind", ["coroutine", "generator", "custom"])
def test_model_source_awaitable_is_closed_before_any_transition(
    tmp_path, awaitable_kind
) -> None:
    events: list[object] = []
    value, is_closed = _awaitable_with_closed_probe(awaitable_kind)

    class _AwaitableModel:
        def __init__(self) -> None:
            self.calls = 0

        def next_action(self, _history):
            self.calls += 1
            return value

    model = _AwaitableModel()
    controller, store = _controller(tmp_path, model, _Adapter(events), events)

    with pytest.raises(TypeError, match="model source returned an awaitable"):
        controller.run()

    assert model.calls == 1
    assert is_closed()
    gc.collect()
    assert store.get_snapshot(_key()).planned is None


@pytest.mark.parametrize("awaitable_kind", ["coroutine", "generator", "custom"])
def test_direct_tool_adapter_awaitable_is_closed_and_never_observed(
    tmp_path,
    awaitable_kind,
) -> None:
    events: list[object] = []
    value, is_closed = _awaitable_with_closed_probe(awaitable_kind)

    class _AwaitableAdapter:
        def execute(self, _invocation):
            return value

    controller, store = _controller(
        tmp_path,
        _Model([_tool("call-1")], events),
        _AwaitableAdapter(),
        events,
    )

    with pytest.raises(AdaptiveLoopToolFailure) as error:
        controller.run()

    assert isinstance(error.value.__cause__, TypeError)
    assert is_closed()
    gc.collect()
    assert store.get_snapshot(_key()).observed is None


@pytest.mark.parametrize("awaitable_kind", ["coroutine", "generator", "custom"])
def test_recovery_awaitable_is_closed_before_observed_write(
    tmp_path, awaitable_kind
) -> None:
    initial_events: list[object] = []
    initial, store = _controller(
        tmp_path,
        _Model([_tool("call-1")], initial_events),
        _Adapter(initial_events, error=TimeoutError("lost result")),
        initial_events,
    )
    with pytest.raises(AdaptiveLoopToolFailure):
        initial.run()

    value, is_closed = _awaitable_with_closed_probe(awaitable_kind)

    class _AwaitableRecoveryAdapter(_Adapter):
        def recover(self, _invocation):
            return value

    resumed_events: list[object] = []
    resumed, _ = _controller(
        tmp_path,
        _Model([_final()], resumed_events),
        _AwaitableRecoveryAdapter(resumed_events),
        resumed_events,
        store=store,
    )

    with pytest.raises(
        AdaptiveLoopIndeterminateError, match="recovery failed"
    ) as error:
        resumed.run()

    assert isinstance(error.value.__cause__, TypeError)
    assert is_closed()
    gc.collect()
    assert store.get_snapshot(_key()).observed is None


@pytest.mark.parametrize("awaitable_kind", ["coroutine", "generator", "custom"])
def test_before_observed_callback_awaitable_is_closed_before_write(
    tmp_path,
    awaitable_kind,
) -> None:
    events: list[object] = []
    value, is_closed = _awaitable_with_closed_probe(awaitable_kind)

    def callback(*_args):
        return value

    controller, store = _controller(
        tmp_path,
        _Model([_tool("call-1")], events),
        _Adapter(events),
        events,
        before_observed=callback,
    )

    with pytest.raises(
        TypeError, match="before_observed callback returned an awaitable"
    ):
        controller.run()

    assert is_closed()
    gc.collect()
    assert store.get_snapshot(_key()).observed is None


@pytest.mark.parametrize("awaitable_kind", ["coroutine", "generator", "custom"])
def test_after_observed_callback_awaitable_is_closed_after_durable_write(
    tmp_path,
    awaitable_kind,
) -> None:
    events: list[object] = []
    value, is_closed = _awaitable_with_closed_probe(awaitable_kind)

    def callback(*_args):
        return value

    controller, store = _controller(
        tmp_path,
        _Model([_tool("call-1")], events),
        _Adapter(events),
        events,
        after_observed=callback,
    )

    with pytest.raises(
        TypeError, match="after_observed callback returned an awaitable"
    ):
        controller.run()

    assert is_closed()
    gc.collect()
    assert store.get_snapshot(_key()).observed is not None


@pytest.mark.parametrize("awaitable_kind", ["coroutine", "generator", "custom"])
@pytest.mark.parametrize(
    "phase",
    [
        AdaptiveActionPhase.PLANNED,
        AdaptiveActionPhase.OBSERVED,
        AdaptiveActionPhase.TERMINAL,
    ],
)
def test_state_transition_callback_awaitable_is_closed_and_resumes_from_durable_state(
    tmp_path,
    awaitable_kind,
    phase,
) -> None:
    raw_store = AdaptiveStateStore(tmp_path / "transition-awaitable.db")
    value, is_closed = _awaitable_with_closed_probe(awaitable_kind)

    class _AwaitableTransitionStore:
        def get_snapshot(self, key):
            return raw_store.get_snapshot(key)

        def on_state_transition(self, key, transition_phase, **kwargs):
            result = raw_store.on_state_transition(key, transition_phase, **kwargs)
            return value if transition_phase is phase else result

    initial_events: list[object] = []
    initial_actions = [_final("done")]
    if phase is not AdaptiveActionPhase.TERMINAL:
        initial_actions = [_tool("call-1")]
    initial_adapter = _Adapter(initial_events)
    initial, _ = _controller(
        tmp_path,
        _Model(initial_actions, initial_events),
        initial_adapter,
        initial_events,
        store=_AwaitableTransitionStore(),
    )

    with pytest.raises(
        TypeError, match="state transition callback returned an awaitable"
    ):
        initial.run()

    assert is_closed()
    gc.collect()
    snapshot = raw_store.get_snapshot(_key())

    resumed_events: list[object] = []
    resumed_model = _Model(
        [] if phase is AdaptiveActionPhase.TERMINAL else [_final("done")],
        resumed_events,
    )
    resumed_adapter = _Adapter(resumed_events)
    resumed, _ = _controller(
        tmp_path,
        resumed_model,
        resumed_adapter,
        resumed_events,
        store=raw_store,
    )

    if phase is AdaptiveActionPhase.PLANNED:
        assert snapshot.planned is not None
        assert snapshot.observed is None
        assert initial_adapter.calls == []
        with pytest.raises(AdaptiveLoopIndeterminateError, match="planned without"):
            resumed.run()
        assert resumed_model.histories == []
        assert resumed_adapter.calls == []
    elif phase is AdaptiveActionPhase.OBSERVED:
        assert snapshot.observed is not None
        assert len(initial_adapter.calls) == 1
        result = resumed.run()
        assert result.answer == "done"
        assert len(result.accepted_results) == 1
        assert resumed_adapter.calls == []
        assert len(resumed_model.histories) == 1
    else:
        assert snapshot.terminal is not None
        assert resumed.run().answer == "done"
        assert resumed_model.histories == []
        assert resumed_adapter.calls == []


def test_final_answer_without_tools_is_durable_and_restart_never_calls_model(
    tmp_path,
) -> None:
    events: list[object] = []
    controller, store = _controller(
        tmp_path, _Model([_final({"done": True})], events), _Adapter(events), events
    )

    result = controller.run()

    assert result.answer == {"done": True}
    terminal = store.get_snapshot(_key()).terminal
    assert terminal is not None
    assert terminal.action == {
        "contract": "adaptive_final_answer_v1",
        "answer": {"done": True},
    }
    resumed_events: list[object] = []
    resumed_model = _Model([], resumed_events)
    resumed, _ = _controller(
        tmp_path, resumed_model, _Adapter(resumed_events), resumed_events, store=store
    )

    assert resumed.run() == result
    assert resumed_model.histories == []


def test_final_answer_after_tools_is_durable_and_restart_never_calls_model(
    tmp_path,
) -> None:
    events: list[object] = []
    controller, store = _controller(
        tmp_path,
        _Model([_tool("call-1"), _tool("call-2"), _final("complete")], events),
        _Adapter(events),
        events,
    )

    result = controller.run()

    terminal = store.get_snapshot(_key(2)).terminal
    assert terminal is not None
    assert terminal.action == {
        "contract": "adaptive_final_answer_v1",
        "answer": "complete",
    }
    resumed_events: list[object] = []
    resumed_model = _Model([], resumed_events)
    resumed, _ = _controller(
        tmp_path, resumed_model, _Adapter(resumed_events), resumed_events, store=store
    )

    assert resumed.run() == result
    assert resumed_model.histories == []


@pytest.mark.parametrize("tool_count", [1, 3])
def test_same_revision_terminal_reconstructs_all_observed_history(
    tmp_path, tool_count
) -> None:
    events: list[object] = []

    def stop_after_last(invocation, _result) -> None:
        if invocation.revision == tool_count - 1:
            raise RuntimeError("stop after last observed")

    initial, store = _controller(
        tmp_path,
        _Model([_tool(f"call-{index}") for index in range(tool_count)], events),
        _Adapter(events),
        events,
        after_observed=stop_after_last,
    )
    with pytest.raises(RuntimeError, match="stop after last observed"):
        initial.run()
    last_revision = tool_count - 1
    store.record_terminal(
        _key(last_revision),
        expected_revision=last_revision,
        action={"contract": "adaptive_final_answer_v1", "answer": "same-revision"},
    )

    resumed_events: list[object] = []
    resumed_model = _Model([], resumed_events)
    resumed, _ = _controller(
        tmp_path,
        resumed_model,
        _Adapter(resumed_events),
        resumed_events,
        store=store,
    )

    result = resumed.run()

    assert result.answer == "same-revision"
    assert [item.tool_call_id for item in result.accepted_results] == [
        f"call-{index}" for index in range(tool_count)
    ]
    assert resumed_model.histories == []


def test_concurrent_matching_final_answers_share_one_terminal_record(tmp_path) -> None:
    store = AdaptiveStateStore(tmp_path / "terminal-race.db")
    barrier = threading.Barrier(2)
    results: list[object] = []

    def finish() -> None:
        events: list[object] = []
        controller, _ = _controller(
            tmp_path,
            _Model([_final("done")], events),
            _Adapter(events),
            events,
            store=store,
        )
        barrier.wait(timeout=10)
        results.append(controller.run())

    workers = [threading.Thread(target=finish) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)

    assert not any(worker.is_alive() for worker in workers)
    assert results == [AdaptiveLoopResult("done", ()), AdaptiveLoopResult("done", ())]
    assert [event.phase.value for event in store.list_events(_key())] == ["terminal"]


def test_resume_above_lower_tool_budget_stops_before_model(tmp_path) -> None:
    events: list[object] = []

    def crash_after_second_observed(invocation, _result) -> None:
        if invocation.revision == 1:
            raise RuntimeError("stop after acceptance")

    initial, store = _controller(
        tmp_path,
        _Model([_tool("call-1"), _tool("call-2")], events),
        _Adapter(events),
        events,
        after_observed=crash_after_second_observed,
    )
    with pytest.raises(RuntimeError, match="stop after acceptance"):
        initial.run()

    resumed_events: list[object] = []
    resumed_model = _Model([_final()], resumed_events)
    resumed, _ = _controller(
        tmp_path,
        resumed_model,
        _Adapter(resumed_events),
        resumed_events,
        store=store,
        max_tool_calls=1,
    )

    with pytest.raises(AdaptiveLoopStepLimitError, match="resumed above"):
        resumed.run()
    assert resumed_model.histories == []


def test_resume_counts_planned_calls_before_recovery_write_or_model(tmp_path) -> None:
    events: list[object] = []

    class _FailSecondAdapter(_Adapter):
        def execute(self, invocation):
            if invocation.revision == 1:
                raise TimeoutError("second result lost")
            return super().execute(invocation)

    initial, store = _controller(
        tmp_path,
        _Model([_tool("call-1"), _tool("call-2")], events),
        _FailSecondAdapter(events),
        events,
    )
    with pytest.raises(AdaptiveLoopToolFailure):
        initial.run()
    assert store.get_snapshot(_key(1)).planned is not None
    assert store.get_snapshot(_key(1)).observed is None

    resumed_events: list[object] = []

    class _RecoveryMustNotRun(_Adapter):
        def recover(self, _invocation):
            raise AssertionError(
                "recovery must not run above the durable planned limit"
            )

    resumed_model = _Model([_final()], resumed_events)
    resumed, _ = _controller(
        tmp_path,
        resumed_model,
        _RecoveryMustNotRun(resumed_events),
        resumed_events,
        store=store,
        max_tool_calls=1,
    )

    with pytest.raises(AdaptiveLoopStepLimitError, match="resumed above"):
        resumed.run()

    assert resumed_events == []
    assert resumed_model.histories == []
    assert store.get_snapshot(_key(1)).observed is None


def test_terminal_snapshot_fails_closed_before_model_call(tmp_path) -> None:
    events: list[object] = []
    store = AdaptiveStateStore(tmp_path / "terminal.db")
    store.record_planned(_key(), expected_revision=None, action={"tool": "inspect"})
    store.record_observed(_key(), expected_revision=0, action={"ok": True})
    store.record_terminal(_key(), expected_revision=0, action={"done": True})
    model = _Model([_final()], events)
    controller, _ = _controller(tmp_path, model, _Adapter(events), events, store=store)

    with pytest.raises(AdaptiveLoopTerminalError, match="terminal checkpoint"):
        controller.run()

    assert model.histories == []


def test_resume_duplicate_id_is_rejected_before_recovery_or_transition(
    tmp_path,
) -> None:
    events: list[object] = []

    class _RecoveringAdapter(_Adapter):
        def recover(self, invocation):
            self.events.append(("recover", invocation.tool_call.tool_call_id))
            return NormalizedToolResult("success", {"recovered": True})

    initial, store = _controller(
        tmp_path, _Model([_tool("call-1")], events), _Adapter(events), events
    )
    with pytest.raises(AdaptiveLoopToolFailure):
        initial._tools.resolve("inspect").error = TimeoutError("lost")
        initial.run()
    first = store.get_snapshot(_key()).planned
    assert first is not None

    class _DuplicateStore:
        def get_snapshot(self, key):
            planned = first if key.revision in {0, 1} else None
            return type(
                "Snapshot", (), {"planned": planned, "observed": None, "terminal": None}
            )()

        def on_state_transition(self, *_args, **_kwargs):
            raise AssertionError("duplicate must be rejected before a transition")

    recovering = _RecoveringAdapter(events)
    resumed, _ = _controller(
        tmp_path,
        _Model([_final()], events),
        recovering,
        events,
        store=_DuplicateStore(),
    )

    with pytest.raises(AdaptiveLoopIndeterminateError, match="repeats"):
        resumed.run()

    assert [event for event in events if event[0] == "recover"] == []
    assert store.get_snapshot(_key()).observed is None


def test_concurrent_controllers_do_not_execute_one_durable_tool_claim_twice(
    tmp_path,
) -> None:
    events: list[object] = []
    entered = threading.Event()
    release = threading.Event()

    class _BlockingAdapter(_Adapter):
        def execute(self, invocation):
            self.calls.append(invocation)
            entered.set()
            assert release.wait(timeout=2)
            return NormalizedToolResult("success", {"owner": "first"})

    store = AdaptiveStateStore(tmp_path / "concurrent.db")
    first_adapter = _BlockingAdapter(events)
    first, _ = _controller(
        tmp_path,
        _Model([_tool("call-1"), _final()], events),
        first_adapter,
        events,
        store=store,
    )
    second_adapter = _Adapter(events)
    second, _ = _controller(
        tmp_path,
        _Model([_tool("call-1"), _final()], events),
        second_adapter,
        events,
        store=store,
    )
    outcome: list[object] = []
    worker = threading.Thread(target=lambda: outcome.append(first.run()))
    worker.start()
    assert entered.wait(timeout=1)
    with pytest.raises(AdaptiveLoopIndeterminateError, match="planned without"):
        second.run()
    release.set()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert len(first_adapter.calls) == 1
    assert second_adapter.calls == []
    assert outcome


def test_crash_before_commit_is_indeterminate_but_crash_after_commit_resumes_without_reexecution(
    tmp_path,
) -> None:
    events: list[object] = []
    model = _Model([_tool("call-1")], events)
    adapter = _Adapter(events)

    def crash_before(*_args) -> None:
        raise RuntimeError("crash before commit")

    controller, store = _controller(
        tmp_path,
        model,
        adapter,
        events,
        before_observed=crash_before,
    )
    with pytest.raises(RuntimeError, match="before commit"):
        controller.run()
    assert store.get_snapshot(_key()).observed is None

    resumed, _ = _controller(
        tmp_path, _Model([_final()], []), _Adapter([]), [], store=store
    )
    with pytest.raises(AdaptiveLoopIndeterminateError):
        resumed.run()

    post_events: list[object] = []
    post_model = _Model([_tool("call-1"), _final()], post_events)
    post_adapter = _Adapter(post_events)

    def crash_after(*_args) -> None:
        raise RuntimeError("crash after commit")

    post_controller, post_store = _controller(
        tmp_path,
        post_model,
        post_adapter,
        post_events,
        store=AdaptiveStateStore(tmp_path / "post.db"),
        after_observed=crash_after,
    )
    with pytest.raises(RuntimeError, match="after commit"):
        post_controller.run()
    assert post_store.get_snapshot(_key()).observed is not None

    resumed_events: list[object] = []
    resumed_adapter = _Adapter(resumed_events)
    resumed_model = _Model([_final("resumed")], resumed_events)
    resumed_post, _ = _controller(
        tmp_path,
        resumed_model,
        resumed_adapter,
        resumed_events,
        store=post_store,
    )
    result = resumed_post.run()
    assert result.answer == "resumed"
    assert resumed_adapter.calls == []
    assert resumed_model.histories[0][0].tool_call_id == "call-1"


def test_idempotent_recovery_adapter_can_finish_a_planned_call_without_reexecution(
    tmp_path,
) -> None:
    events: list[object] = []

    class _RecoveringAdapter(_Adapter):
        def recover(self, invocation):
            self.events.append(("recover", invocation.tool_call.tool_call_id))
            return NormalizedToolResult("success", {"recovered": True})

    initial = _RecoveringAdapter(events, error=TimeoutError("lost response"))
    failed_model = _Model([_tool("call-1")], events)
    failed, store = _controller(tmp_path, failed_model, initial, events)
    with pytest.raises(AdaptiveLoopToolFailure):
        failed.run()

    resumed_events: list[object] = []
    recovering = _RecoveringAdapter(resumed_events)
    resumed_model = _Model([_final("recovered")], resumed_events)
    resumed, _ = _controller(
        tmp_path,
        resumed_model,
        recovering,
        resumed_events,
        store=store,
    )
    result = resumed.run()

    assert result.answer == "recovered"
    assert recovering.calls == []
    assert resumed_events == [
        ("recover", "call-1"),
        ("commit", "observed", 0),
        ("model", 1),
        ("commit", "terminal", 1),
    ]
    assert store.get_snapshot(_key()).observed.action["value"] == {"recovered": True}


def test_serial_loop_rejects_code_agent_and_enforces_explicit_limit(tmp_path) -> None:
    with pytest.raises(
        AdaptiveLoopUnsupportedAgentError, match="only explicit tool_calling"
    ):
        require_tool_calling_agent("code")
    require_tool_calling_agent("tool_calling")

    events: list[object] = []
    model = _Model([_tool("call-1"), _tool("call-2")], events)
    controller, _ = _controller(
        tmp_path,
        model,
        _Adapter(events),
        events,
        max_tool_calls=1,
    )
    with pytest.raises(AdaptiveLoopStepLimitError):
        controller.run()
