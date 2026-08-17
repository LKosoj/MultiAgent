"""Small durable serial loop for adaptive Text-to-SQL tool calls.

The controller deliberately does not reuse ``smolagents`` internal loops.
It owns only the narrow boundary where a model action becomes one accepted
tool result.  Existing model profiles and tools can be supplied through the
protocols below; wiring them into the production pipeline is a later task.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import inspect
import math
import re
from typing import Any, Literal, Protocol, TypeAlias

from workflow.adaptive_state_store import (
    AdaptiveActionPhase,
    AdaptiveCheckpointCasError,
    AdaptiveCheckpointKey,
    AdaptiveLoopKind,
)
from workflow.deadline import DeadlineBudget

from .serialization import canonical_digest, canonical_json_bytes


JsonValue: TypeAlias = (
    None | bool | int | float | str | tuple["JsonValue", ...] | dict[str, "JsonValue"]
)
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_NO_TERMINAL_ANSWER = object()
MAX_MODEL_ACTION_BYTES = 256 * 1024


class AdaptiveLoopError(RuntimeError):
    """Base error for the narrow adaptive tool-loop boundary."""


class AdaptiveLoopActionError(AdaptiveLoopError):
    """The model action is not the closed, serial tool-call shape."""


class AdaptiveLoopUnknownToolError(AdaptiveLoopError):
    """The model requested a tool outside the supplied allowlist."""


class AdaptiveLoopToolFailure(AdaptiveLoopError):
    """The tool failed before producing a normalized result."""


class AdaptiveLoopCancelled(AdaptiveLoopError):
    """Cancellation won before a result became durable."""


class AdaptiveLoopIndeterminateError(AdaptiveLoopError):
    """A prior tool call was planned but its result was not made durable."""


class AdaptiveLoopTerminalError(AdaptiveLoopError):
    """A durable terminal checkpoint prevents another controller turn."""


class AdaptiveLoopUnsupportedAgentError(AdaptiveLoopError):
    """The requested agent kind has no trustworthy tool-result boundary."""


class AdaptiveLoopStepLimitError(AdaptiveLoopError):
    """The serial loop exhausted its explicitly configured action budget."""


@dataclass(frozen=True, slots=True)
class LoopMessage:
    """One immutable item supplied to the next model turn."""

    kind: Literal["tool_result"]
    tool_call_id: str
    tool_name: str
    arguments: dict[str, JsonValue]
    result: "NormalizedToolResult"


@dataclass(frozen=True, slots=True)
class ToolCall:
    """Strictly parsed single tool action from one model turn."""

    tool_call_id: str
    tool_name: str
    arguments: dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class FinalAnswer:
    """Strictly parsed final model output."""

    answer: JsonValue


@dataclass(frozen=True, slots=True)
class NormalizedToolResult:
    """A tool outcome that is safe to give back to the model.

    ``status='error'`` is intentional: an adapter may turn a recoverable tool
    failure into an ordinary model-visible result.  An exception is not an
    accepted result and therefore is never recorded as ``OBSERVED``.
    """

    status: Literal["success", "error"]
    value: JsonValue


@dataclass(frozen=True, slots=True)
class ToolInvocation:
    """All stable data an adapter receives for one serial execution."""

    run_id: str
    run_incarnation: str
    loop_kind: AdaptiveLoopKind
    revision: int
    tool_call: ToolCall
    invocation_id: str
    remaining_seconds: float | None
    deadline: DeadlineBudget | None


@dataclass(frozen=True, slots=True)
class AdaptiveLoopResult:
    """Completed answer and all unique accepted results in model order."""

    answer: JsonValue
    accepted_results: tuple[LoopMessage, ...]


class ModelActionSource(Protocol):
    """Produces exactly one raw action after seeing committed tool results."""

    def next_action(self, history: tuple[LoopMessage, ...]) -> Mapping[str, object]: ...


class ToolAdapter(Protocol):
    """Executes one call and receives the remaining absolute deadline budget."""

    def execute(self, invocation: ToolInvocation) -> NormalizedToolResult: ...


class IdempotentRecoveryToolAdapter(ToolAdapter, Protocol):
    """Optionally obtains a prior invocation outcome without executing it again."""

    def recover(self, invocation: ToolInvocation) -> NormalizedToolResult | None: ...


class ToolResolver(Protocol):
    """Maps an allowlisted name to a tool adapter, or returns ``None``."""

    def resolve(self, tool_name: str) -> ToolAdapter | None: ...


class StateTransitionRecorder(Protocol):
    """The existing durable adaptive-state journal callback."""

    def on_state_transition(
        self,
        key: AdaptiveCheckpointKey,
        phase: AdaptiveActionPhase,
        *,
        expected_revision: int | None,
        action: Any,
        artifact_digest: str | None = None,
    ) -> Any: ...

    def get_snapshot(self, key: AdaptiveCheckpointKey) -> Any: ...


class MappingToolResolver:
    """Adapter for an already resolved ToolRegistry allowlist."""

    def __init__(self, tools: Mapping[str, ToolAdapter]) -> None:
        self._tools = dict(tools)

    def resolve(self, tool_name: str) -> ToolAdapter | None:
        return self._tools.get(tool_name)


class CallableToolAdapter:
    """Adapter for a synchronous callable returned by an existing registry."""

    def __init__(self, tool: Callable[..., object]) -> None:
        if not callable(tool):
            raise TypeError("tool must be callable")
        if inspect.iscoroutinefunction(tool) or inspect.iscoroutinefunction(
            getattr(tool, "__call__", None)
        ):
            raise TypeError("async tools are not supported")
        self._tool = tool

    def execute(self, invocation: ToolInvocation) -> NormalizedToolResult:
        value = self._tool(**invocation.tool_call.arguments)
        _reject_awaitable(value, "callable tool")
        return NormalizedToolResult("success", _json_value(value, "tool result"))


class ToolManagerAdapter:
    """Adapter for a callable executed through the existing ToolManager.

    The manager still owns telemetry and run tracking.  The supplied callable
    remains synchronous.  ``deadline`` is forwarded to tools that declare
    that existing Text-to-SQL parameter; others still receive the controller's
    boundary checks but cannot be forcibly stopped by a generic thread wrapper.
    """

    def __init__(
        self,
        *,
        tool_manager: Any,
        tool: Callable[..., object],
        task_description: str,
        session_id: str | None = None,
    ) -> None:
        if not callable(getattr(tool_manager, "run_tool", None)):
            raise TypeError("tool_manager must provide run_tool")
        if not callable(tool):
            raise TypeError("tool must be callable")
        if inspect.iscoroutinefunction(tool) or inspect.iscoroutinefunction(
            getattr(tool, "__call__", None)
        ):
            raise TypeError("async tools are not supported")
        if type(task_description) is not str or not task_description:
            raise ValueError("task_description must be non-empty text")
        self._tool_manager = tool_manager
        self._tool = tool
        self._task_description = task_description
        self._session_id = session_id

    def execute(self, invocation: ToolInvocation) -> NormalizedToolResult:
        call_arguments = dict(invocation.tool_call.arguments)
        call_arguments["deadline"] = invocation.deadline
        value = self._tool_manager.run_tool(
            tool_name=invocation.tool_call.tool_name,
            tool_function=self._tool,
            task_description=self._task_description,
            session_id=self._session_id,
            workflow_run_id=invocation.run_id,
            tool_invocation_id=invocation.invocation_id,
            **call_arguments,
        )
        _reject_awaitable(value, "tool manager")
        return NormalizedToolResult("success", _json_value(value, "tool result"))


def require_tool_calling_agent(agent_kind: str) -> None:
    """Fail closed instead of inventing CodeAgent tool-result semantics."""

    if agent_kind != "tool_calling":
        raise AdaptiveLoopUnsupportedAgentError(
            "adaptive controller supports only explicit tool_calling agents"
        )


class AdaptiveLoopController:
    """Serial model → tool → durable-result loop with crash-safe boundaries."""

    def __init__(
        self,
        *,
        model: ModelActionSource,
        tools: ToolResolver,
        state_store: StateTransitionRecorder,
        run_id: str,
        run_incarnation: str,
        loop_kind: AdaptiveLoopKind,
        deadline: DeadlineBudget | None = None,
        is_cancelled: Callable[[], bool] | None = None,
        max_tool_calls: int = 20,
        max_model_turns: int = 40,
        before_observed: Callable[[ToolInvocation, NormalizedToolResult], None]
        | None = None,
        after_observed: Callable[[ToolInvocation, NormalizedToolResult], None]
        | None = None,
    ) -> None:
        if not callable(getattr(model, "next_action", None)):
            raise TypeError("model must provide next_action")
        if not callable(getattr(tools, "resolve", None)):
            raise TypeError("tools must provide resolve")
        if not callable(getattr(state_store, "on_state_transition", None)):
            raise TypeError("state_store must provide on_state_transition")
        if not callable(getattr(state_store, "get_snapshot", None)):
            raise TypeError("state_store must provide get_snapshot")
        if type(run_id) is not str or not run_id:
            raise ValueError("run_id must be non-empty text")
        if type(run_incarnation) is not str or not run_incarnation:
            raise ValueError("run_incarnation must be non-empty text")
        if not isinstance(loop_kind, AdaptiveLoopKind):
            raise TypeError("loop_kind must be AdaptiveLoopKind")
        if deadline is not None and not isinstance(deadline, DeadlineBudget):
            raise TypeError("deadline must be DeadlineBudget or null")
        if is_cancelled is not None and not callable(is_cancelled):
            raise TypeError("is_cancelled must be callable or null")
        if type(max_tool_calls) is not int or max_tool_calls <= 0:
            raise ValueError("max_tool_calls must be a positive integer")
        if type(max_model_turns) is not int or max_model_turns <= 0:
            raise ValueError("max_model_turns must be a positive integer")
        if before_observed is not None and not callable(before_observed):
            raise TypeError("before_observed must be callable or null")
        if after_observed is not None and not callable(after_observed):
            raise TypeError("after_observed must be callable or null")
        self._model = model
        self._tools = tools
        self._state_store = state_store
        self._run_id = run_id
        self._run_incarnation = run_incarnation
        self._loop_kind = loop_kind
        self._deadline = deadline
        self._is_cancelled = is_cancelled or (lambda: False)
        self._max_tool_calls = max_tool_calls
        self._max_model_turns = max_model_turns
        self._before_observed = before_observed
        self._after_observed = after_observed

    def run(self) -> AdaptiveLoopResult:
        """Run until the model returns a strict final action.

        A persisted ``OBSERVED`` record is added to history before another
        model request.  A persisted ``PLANNED`` record without ``OBSERVED`` is
        recovered only through an ``IdempotentRecoveryToolAdapter`` with the
        matching durable result; without either, resume is indeterminate.
        """

        history, revision, accepted, terminal_result = self._resume_history()
        if terminal_result is not None:
            return terminal_result
        model_turns = 0
        while True:
            if model_turns >= self._max_model_turns:
                raise AdaptiveLoopStepLimitError(
                    "adaptive loop reached max_model_turns"
                )
            self._require_active("model request")
            model_turns += 1
            raw_action = self._model.next_action(
                tuple(_detached_message(message) for message in history)
            )
            _reject_awaitable(raw_action, "model source")
            self._require_active("model response")
            action = parse_model_action(raw_action)
            if isinstance(action, FinalAnswer):
                terminal_action = _terminal_action(action)
                durable_snapshot = self._record_transition(
                    self._key(revision),
                    AdaptiveActionPhase.TERMINAL,
                    expected_revision=None if revision == 0 else revision - 1,
                    action=terminal_action,
                )
                terminal = getattr(durable_snapshot, "terminal", None)
                if terminal is None:
                    raise AdaptiveLoopIndeterminateError(
                        "durable terminal response is missing"
                    )
                answer = _terminal_answer(terminal.action)
                if answer is _NO_TERMINAL_ANSWER:
                    raise AdaptiveLoopIndeterminateError(
                        "durable terminal response is invalid"
                    )
                return AdaptiveLoopResult(
                    _detached_json(answer),
                    tuple(_detached_message(message) for message in accepted),
                )

            duplicate = _find_accepted(accepted, action.tool_call_id)
            if duplicate is not None:
                _require_same_call(duplicate, action)
                history.append(duplicate)
                continue

            if len(accepted) >= self._max_tool_calls:
                raise AdaptiveLoopStepLimitError("adaptive loop reached max_tool_calls")

            adapter = self._tools.resolve(action.tool_name)
            if adapter is None:
                raise AdaptiveLoopUnknownToolError(f"unknown tool: {action.tool_name}")
            self._require_active("tool planning")
            key = self._key(revision)
            invocation = ToolInvocation(
                run_id=self._run_id,
                run_incarnation=self._run_incarnation,
                loop_kind=self._loop_kind,
                revision=revision,
                tool_call=action,
                invocation_id=_invocation_id(
                    self._run_id,
                    self._run_incarnation,
                    self._loop_kind,
                    action.tool_call_id,
                ),
                remaining_seconds=self._remaining("tool planning"),
                deadline=self._deadline,
            )
            planned_action = _planned_action(invocation)
            try:
                self._record_transition(
                    key,
                    AdaptiveActionPhase.PLANNED,
                    expected_revision=None if revision == 0 else revision - 1,
                    action=planned_action,
                )
            except AdaptiveCheckpointCasError as exc:
                raise AdaptiveLoopIndeterminateError(
                    "tool call is already durably owned by another controller"
                ) from exc
            self._require_active("tool execution")
            try:
                result = adapter.execute(_detached_invocation(invocation))
                _reject_awaitable(result, "tool adapter")
            except Exception as exc:
                raise AdaptiveLoopToolFailure(
                    f"tool {action.tool_name!r} failed before acceptance"
                ) from exc
            _assert_same_json(
                planned_action, _planned_action(invocation), "tool adapter"
            )
            result = _validated_result(result)
            self._require_active("tool result acceptance")
            observed_action = _observed_action(invocation, result)
            if self._before_observed is not None:
                callback_result = self._before_observed(
                    _detached_invocation(invocation),
                    _detached_result(result),
                )
                _reject_awaitable(callback_result, "before_observed callback")
            _assert_same_json(
                observed_action,
                _observed_action(invocation, result),
                "before_observed callback",
            )
            self._require_active("durable result acceptance")
            durable_snapshot = self._record_transition(
                key,
                AdaptiveActionPhase.OBSERVED,
                expected_revision=revision,
                action=observed_action,
            )
            message = LoopMessage(
                "tool_result",
                action.tool_call_id,
                action.tool_name,
                action.arguments,
                result,
            )
            durable_message = _message_from_events(
                durable_snapshot.planned.action,
                durable_snapshot.observed.action,
            )
            if durable_message != message:
                raise AdaptiveLoopIndeterminateError(
                    "durable tool result does not match the accepted result"
                )
            history.append(message)
            accepted.append(message)
            if self._after_observed is not None:
                callback_result = self._after_observed(
                    _detached_invocation(invocation),
                    _detached_result(result),
                )
                _reject_awaitable(callback_result, "after_observed callback")
            _assert_same_json(
                observed_action,
                _observed_action(invocation, result),
                "after_observed callback",
            )
            revision += 1

    def _resume_history(
        self,
    ) -> tuple[list[LoopMessage], int, list[LoopMessage], AdaptiveLoopResult | None]:
        history: list[LoopMessage] = []
        accepted: list[LoopMessage] = []
        pending: list[tuple[int, object, object | None]] = []
        seen_ids: set[str] = set()
        revision = 0
        terminal_action: object | None = None
        while True:
            self._require_active("resume")
            snapshot = self._state_store.get_snapshot(self._key(revision))
            planned = getattr(snapshot, "planned", None)
            observed = getattr(snapshot, "observed", None)
            terminal = getattr(snapshot, "terminal", None)
            if terminal is not None:
                terminal_action = terminal.action
                if _terminal_answer(terminal_action) is _NO_TERMINAL_ANSWER:
                    raise AdaptiveLoopTerminalError(
                        "durable terminal checkpoint prevents controller resume"
                    )
            if planned is None:
                break
            invocation = _invocation_from_planned(
                planned.action,
                run_id=self._run_id,
                run_incarnation=self._run_incarnation,
                loop_kind=self._loop_kind,
                revision=revision,
                remaining_seconds=self._remaining("resume"),
                deadline=self._deadline,
            )
            if invocation.tool_call.tool_call_id in seen_ids:
                raise AdaptiveLoopIndeterminateError(
                    "durable history repeats a tool_call_id"
                )
            seen_ids.add(invocation.tool_call.tool_call_id)
            pending.append(
                (revision, planned.action, observed.action if observed else None)
            )
            revision += 1
            if terminal is not None:
                break

        if len(pending) > self._max_tool_calls:
            raise AdaptiveLoopStepLimitError(
                "adaptive loop resumed above max_tool_calls"
            )
        if terminal_action is not None and any(item[2] is None for item in pending):
            raise AdaptiveLoopIndeterminateError(
                "durable terminal checkpoint has an unobserved tool call"
            )
        for item_revision, planned_action, observed_action in pending:
            if observed_action is None:
                message = self._recover_unobserved_planned(
                    item_revision, planned_action
                )
            else:
                message = _message_from_events(planned_action, observed_action)
            history.append(message)
            accepted.append(message)
        if terminal_action is not None:
            answer = _terminal_answer(terminal_action)
            if answer is _NO_TERMINAL_ANSWER:
                raise AdaptiveLoopTerminalError(
                    "durable terminal checkpoint prevents controller resume"
                )
            return (
                history,
                revision,
                accepted,
                AdaptiveLoopResult(
                    _detached_json(answer),
                    tuple(_detached_message(message) for message in accepted),
                ),
            )
        return history, revision, accepted, None

    def _recover_unobserved_planned(
        self, revision: int, planned_action: object
    ) -> LoopMessage:
        self._require_active("tool recovery")
        invocation = _invocation_from_planned(
            planned_action,
            run_id=self._run_id,
            run_incarnation=self._run_incarnation,
            loop_kind=self._loop_kind,
            revision=revision,
            remaining_seconds=self._remaining("tool recovery"),
            deadline=self._deadline,
        )
        adapter = self._tools.resolve(invocation.tool_call.tool_name)
        recover = getattr(adapter, "recover", None) if adapter is not None else None
        if not callable(recover):
            raise AdaptiveLoopIndeterminateError(
                f"tool call at revision {revision} was planned without a durable result"
            )
        planned_snapshot = _planned_action(invocation)
        try:
            recovered = recover(_detached_invocation(invocation))
            _reject_awaitable(recovered, "tool recovery adapter")
        except Exception as exc:
            raise AdaptiveLoopIndeterminateError(
                f"tool call at revision {revision} recovery failed"
            ) from exc
        _assert_same_json(
            planned_snapshot, _planned_action(invocation), "tool recovery adapter"
        )
        if recovered is None:
            raise AdaptiveLoopIndeterminateError(
                f"tool call at revision {revision} recovery has no idempotent result"
            )
        result = _validated_result(recovered)
        self._require_active("recovered result acceptance")
        if self._before_observed is not None:
            callback_result = self._before_observed(
                _detached_invocation(invocation),
                _detached_result(result),
            )
            _reject_awaitable(callback_result, "before_observed callback")
        observed_action = _observed_action(invocation, result)
        _assert_same_json(
            observed_action,
            _observed_action(invocation, result),
            "before_observed callback",
        )
        self._require_active("durable recovered result acceptance")
        durable_snapshot = self._record_transition(
            self._key(revision),
            AdaptiveActionPhase.OBSERVED,
            expected_revision=revision,
            action=observed_action,
        )
        message = LoopMessage(
            "tool_result",
            invocation.tool_call.tool_call_id,
            invocation.tool_call.tool_name,
            invocation.tool_call.arguments,
            result,
        )
        if (
            _message_from_events(
                durable_snapshot.planned.action,
                durable_snapshot.observed.action,
            )
            != message
        ):
            raise AdaptiveLoopIndeterminateError(
                "durable recovered result does not match the accepted result"
            )
        if self._after_observed is not None:
            callback_result = self._after_observed(
                _detached_invocation(invocation),
                _detached_result(result),
            )
            _reject_awaitable(callback_result, "after_observed callback")
        _assert_same_json(
            observed_action,
            _observed_action(invocation, result),
            "after_observed callback",
        )
        return message

    def _record_transition(
        self,
        key: AdaptiveCheckpointKey,
        phase: AdaptiveActionPhase,
        *,
        expected_revision: int | None,
        action: dict[str, JsonValue],
    ) -> Any:
        transition_result = self._state_store.on_state_transition(
            key,
            phase,
            expected_revision=expected_revision,
            action=_detached_json(action),
        )
        _reject_awaitable(transition_result, "state transition callback")
        snapshot = self._state_store.get_snapshot(key)
        event = getattr(snapshot, phase.value, None)
        if event is None:
            raise AdaptiveLoopIndeterminateError("durable state transition is missing")
        _assert_same_json(action, event.action, "durable state transition")
        return snapshot

    def _key(self, revision: int) -> AdaptiveCheckpointKey:
        return AdaptiveCheckpointKey(
            self._run_id,
            self._run_incarnation,
            self._loop_kind,
            revision,
        )

    def _require_active(self, boundary: str) -> None:
        if self._is_cancelled():
            raise AdaptiveLoopCancelled(f"adaptive loop cancelled before {boundary}")
        self._remaining(boundary)

    def _remaining(self, boundary: str) -> float | None:
        if self._deadline is None:
            return None
        return self._deadline.require_remaining(boundary)


def parse_model_action(value: Mapping[str, object]) -> ToolCall | FinalAnswer:
    """Decode one closed action; arrays of calls are rejected in serial mode."""

    if not isinstance(value, Mapping):
        raise AdaptiveLoopActionError("model action must be an object")
    kind = value.get("kind")
    if kind == "tool":
        _require_exact_keys(value, {"kind", "tool_call_id", "tool_name", "arguments"})
        action = ToolCall(
            tool_call_id=_identifier(value["tool_call_id"], "tool_call_id"),
            tool_name=_identifier(value["tool_name"], "tool_name"),
            arguments=_json_object(value["arguments"], "arguments"),
        )
        _require_model_action_byte_bound(
            {
                "kind": "tool",
                "tool_call_id": action.tool_call_id,
                "tool_name": action.tool_name,
                "arguments": action.arguments,
            }
        )
        return action
    if kind == "final":
        _require_exact_keys(value, {"kind", "answer"})
        action = FinalAnswer(_json_value(value["answer"], "answer"))
        _require_model_action_byte_bound({"kind": "final", "answer": action.answer})
        return action
    raise AdaptiveLoopActionError("model action kind must be 'tool' or 'final'")


def _require_exact_keys(value: Mapping[str, object], expected: set[str]) -> None:
    if set(value) != expected:
        raise AdaptiveLoopActionError(
            "model action fields must match the closed contract"
        )


def _require_model_action_byte_bound(action: Mapping[str, JsonValue]) -> None:
    if len(canonical_json_bytes(action)) > MAX_MODEL_ACTION_BYTES:
        raise AdaptiveLoopActionError("model action exceeds its canonical byte bound")


def _identifier(value: object, field_name: str) -> str:
    if type(value) is not str or not _IDENTIFIER_RE.fullmatch(value):
        raise AdaptiveLoopActionError(f"{field_name} must be a supported identifier")
    return value


def _json_object(value: object, label: str) -> dict[str, JsonValue]:
    if not isinstance(value, Mapping):
        raise AdaptiveLoopActionError(f"{label} must be an object")
    if any(type(key) is not str for key in value):
        raise AdaptiveLoopActionError(f"{label} object keys must be text")
    return {key: _json_value(item, label) for key, item in value.items()}


def _json_value(value: object, label: str) -> JsonValue:
    try:
        _reject_awaitable(value, label)
    except TypeError as exc:
        raise AdaptiveLoopActionError(f"{label} must be synchronous") from exc
    if value is None or type(value) in {bool, int, str}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise AdaptiveLoopActionError(f"{label} must contain finite JSON numbers")
        return value
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise AdaptiveLoopActionError(f"{label} object keys must be text")
        return {key: _json_value(item, label) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return tuple(_json_value(item, label) for item in value)
    raise AdaptiveLoopActionError(f"{label} must contain only JSON values")


def _validated_result(value: object) -> NormalizedToolResult:
    if not isinstance(value, NormalizedToolResult):
        raise AdaptiveLoopToolFailure("tool adapter must return NormalizedToolResult")
    if value.status not in {"success", "error"}:
        raise AdaptiveLoopToolFailure("tool result status is invalid")
    try:
        normalized_value = _json_value(value.value, "tool result")
    except AdaptiveLoopActionError as exc:
        raise AdaptiveLoopToolFailure("tool result is not normalized JSON") from exc
    return NormalizedToolResult(value.status, normalized_value)


def _reject_awaitable(value: object, boundary: str) -> None:
    """Close and reject an awaitable returned through a synchronous boundary."""

    if not inspect.isawaitable(value):
        return
    close = getattr(value, "close", None)
    if callable(close):
        close_result = close()
        if close_result is not value and inspect.isawaitable(close_result):
            nested_close = getattr(close_result, "close", None)
            if callable(nested_close):
                nested_close()
    raise TypeError(f"{boundary} returned an awaitable")


def _detached_json(value: JsonValue) -> JsonValue:
    return _json_value(value, "JSON value")


def _detached_result(result: NormalizedToolResult) -> NormalizedToolResult:
    return NormalizedToolResult(result.status, _detached_json(result.value))


def _detached_invocation(invocation: ToolInvocation) -> ToolInvocation:
    tool_call = invocation.tool_call
    return ToolInvocation(
        run_id=invocation.run_id,
        run_incarnation=invocation.run_incarnation,
        loop_kind=invocation.loop_kind,
        revision=invocation.revision,
        tool_call=ToolCall(
            tool_call.tool_call_id,
            tool_call.tool_name,
            _json_object(tool_call.arguments, "tool arguments"),
        ),
        invocation_id=invocation.invocation_id,
        remaining_seconds=invocation.remaining_seconds,
        deadline=invocation.deadline,
    )


def _detached_message(message: LoopMessage) -> LoopMessage:
    return LoopMessage(
        message.kind,
        message.tool_call_id,
        message.tool_name,
        _json_object(message.arguments, "tool arguments"),
        _detached_result(message.result),
    )


def _assert_same_json(expected: JsonValue, actual: object, boundary: str) -> None:
    try:
        actual_value = _json_value(actual, boundary)
    except AdaptiveLoopActionError as exc:
        raise AdaptiveLoopIndeterminateError(
            f"{boundary} changed an internal value"
        ) from exc
    if canonical_digest(expected) != canonical_digest(actual_value):
        raise AdaptiveLoopIndeterminateError(f"{boundary} changed an internal value")


def _invocation_id(
    run_id: str,
    run_incarnation: str,
    loop_kind: AdaptiveLoopKind,
    tool_call_id: str,
) -> str:
    return (
        "invoke:"
        + canonical_digest(
            {
                "run_id": run_id,
                "run_incarnation": run_incarnation,
                "loop_kind": loop_kind.value,
                "tool_call_id": tool_call_id,
            }
        ).split(":", 1)[1]
    )


def _planned_action(invocation: ToolInvocation) -> dict[str, JsonValue]:
    return {
        "contract": "adaptive_tool_call_v1",
        "tool_call_id": invocation.tool_call.tool_call_id,
        "tool_name": invocation.tool_call.tool_name,
        "arguments": invocation.tool_call.arguments,
        "invocation_id": invocation.invocation_id,
    }


def _observed_action(
    invocation: ToolInvocation,
    result: NormalizedToolResult,
) -> dict[str, JsonValue]:
    return {
        "contract": "adaptive_tool_result_v1",
        "tool_call_id": invocation.tool_call.tool_call_id,
        "tool_name": invocation.tool_call.tool_name,
        "invocation_id": invocation.invocation_id,
        "status": result.status,
        "value": result.value,
    }


def _terminal_action(answer: FinalAnswer) -> dict[str, JsonValue]:
    return {
        "contract": "adaptive_final_answer_v1",
        "answer": answer.answer,
    }


def _terminal_answer(action: object) -> JsonValue | object:
    """Read the canonical final response; legacy terminal markers remain closed."""

    if (
        not isinstance(action, Mapping)
        or action.get("contract") != "adaptive_final_answer_v1"
    ):
        return _NO_TERMINAL_ANSWER
    try:
        if set(action) != {"contract", "answer"}:
            raise ValueError
        return _json_value(action["answer"], "durable terminal answer")
    except (KeyError, ValueError, AdaptiveLoopActionError):
        raise AdaptiveLoopIndeterminateError(
            "durable terminal response is invalid"
        ) from None


def _message_from_events(planned: object, observed: object) -> LoopMessage:
    if not isinstance(planned, Mapping) or not isinstance(observed, Mapping):
        raise AdaptiveLoopIndeterminateError("durable action shape is invalid")
    try:
        if planned.get("contract") != "adaptive_tool_call_v1":
            raise ValueError
        if observed.get("contract") != "adaptive_tool_result_v1":
            raise ValueError
        tool_call_id = _identifier(planned["tool_call_id"], "durable tool_call_id")
        tool_name = _identifier(planned["tool_name"], "durable tool_name")
        if (
            observed.get("tool_call_id") != tool_call_id
            or observed.get("tool_name") != tool_name
        ):
            raise ValueError
        if observed.get("invocation_id") != planned.get("invocation_id"):
            raise ValueError
        status = observed["status"]
        if status not in {"success", "error"}:
            raise ValueError
        return LoopMessage(
            "tool_result",
            tool_call_id,
            tool_name,
            _json_object(planned["arguments"], "durable arguments"),
            NormalizedToolResult(
                status, _json_value(observed["value"], "durable result")
            ),
        )
    except (KeyError, TypeError, ValueError, AdaptiveLoopActionError):
        raise AdaptiveLoopIndeterminateError("durable tool result is invalid") from None


def _invocation_from_planned(
    planned: object,
    *,
    run_id: str,
    run_incarnation: str,
    loop_kind: AdaptiveLoopKind,
    revision: int,
    remaining_seconds: float | None,
    deadline: DeadlineBudget | None,
) -> ToolInvocation:
    if not isinstance(planned, Mapping):
        raise AdaptiveLoopIndeterminateError("durable planned tool call is invalid")
    try:
        if planned.get("contract") != "adaptive_tool_call_v1":
            raise ValueError
        tool_call = ToolCall(
            _identifier(planned["tool_call_id"], "durable tool_call_id"),
            _identifier(planned["tool_name"], "durable tool_name"),
            _json_object(planned["arguments"], "durable arguments"),
        )
        invocation_id = _identifier(planned["invocation_id"], "durable invocation_id")
        expected_id = _invocation_id(
            run_id, run_incarnation, loop_kind, tool_call.tool_call_id
        )
        if invocation_id != expected_id:
            raise ValueError
    except (KeyError, ValueError, AdaptiveLoopActionError):
        raise AdaptiveLoopIndeterminateError(
            "durable planned tool call is invalid"
        ) from None
    return ToolInvocation(
        run_id=run_id,
        run_incarnation=run_incarnation,
        loop_kind=loop_kind,
        revision=revision,
        tool_call=tool_call,
        invocation_id=invocation_id,
        remaining_seconds=remaining_seconds,
        deadline=deadline,
    )


def _find_accepted(
    messages: list[LoopMessage], tool_call_id: str
) -> LoopMessage | None:
    return next(
        (message for message in messages if message.tool_call_id == tool_call_id), None
    )


def _require_same_call(message: LoopMessage, call: ToolCall) -> None:
    if message.tool_name != call.tool_name or canonical_digest(
        message.arguments
    ) != canonical_digest(call.arguments):
        raise AdaptiveLoopActionError(
            "duplicate tool_call_id has different call semantics"
        )
