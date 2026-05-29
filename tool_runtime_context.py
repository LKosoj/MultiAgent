"""Runtime metadata available to tool calls inside workflow agent steps."""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Any, Mapping


_TOOL_RUNTIME_CONTEXT: ContextVar[dict[str, Any] | None] = ContextVar(
    "tool_runtime_context",
    default=None,
)


def set_tool_runtime_context(values: Mapping[str, Any] | None) -> Token[dict[str, Any] | None]:
    """Set per-thread workflow metadata for tools executed by an agent."""
    return _TOOL_RUNTIME_CONTEXT.set(dict(values or {}))


def reset_tool_runtime_context(token: Token[dict[str, Any] | None]) -> None:
    """Restore previous runtime metadata after the agent step finishes."""
    _TOOL_RUNTIME_CONTEXT.reset(token)


def get_tool_runtime_value(key: str, default: Any = None) -> Any:
    """Read a workflow metadata value for the current tool call."""
    context = _TOOL_RUNTIME_CONTEXT.get()
    if not isinstance(context, dict):
        return default
    return context.get(key, default)
