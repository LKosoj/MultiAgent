"""Runtime metadata available to tool calls inside workflow agent steps."""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Mapping

if TYPE_CHECKING:
    from custom_tools.text_to_sql.validators import TextToSqlSafetyPolicy
    from workflow.deadline import DeadlineBudget


_TOOL_RUNTIME_CONTEXT: ContextVar[dict[str, Any] | None] = ContextVar(
    "tool_runtime_context",
    default=None,
)


@dataclass(frozen=True, slots=True)
class SupervisorExecutionEvidence:
    """Private worker claim proving that a killable supervisor owns this run."""

    supervisor_id: str
    attempt_generation: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.supervisor_id, str)
            or not self.supervisor_id
            or self.supervisor_id != self.supervisor_id.strip()
            or len(self.supervisor_id) > 512
            or not all(character.isprintable() for character in self.supervisor_id)
        ):
            raise ValueError("supervisor_id must be canonical text")
        if (
            isinstance(self.attempt_generation, bool)
            or not isinstance(self.attempt_generation, int)
            or self.attempt_generation <= 0
        ):
            raise ValueError("attempt_generation must be a positive integer")


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


def get_runtime_context_safety_policy() -> TextToSqlSafetyPolicy | None:
    """Return the request policy only after the worker has rehydrated it."""
    value = get_tool_runtime_value("safety_policy")
    if value is None:
        return None

    from custom_tools.text_to_sql.validators import TextToSqlSafetyPolicy

    if not isinstance(value, TextToSqlSafetyPolicy):
        raise TypeError(
            "runtime safety_policy must be a TextToSqlSafetyPolicy instance"
        )
    return value


def get_runtime_context_deadline() -> DeadlineBudget | None:
    """Return the workflow deadline admitted for the current tool call."""
    value = get_tool_runtime_value("deadline_budget")
    if value is None:
        return None

    from workflow.deadline import DeadlineBudget

    if not isinstance(value, DeadlineBudget):
        raise TypeError(
            "runtime deadline_budget must be a DeadlineBudget instance"
        )
    return value


def get_runtime_context_supervisor_evidence() -> SupervisorExecutionEvidence | None:
    """Return the validated private supervisor claim for this worker attempt."""
    value = get_tool_runtime_value("supervisor_evidence")
    if value is None:
        return None
    if not isinstance(value, SupervisorExecutionEvidence):
        raise TypeError(
            "runtime supervisor_evidence must be a SupervisorExecutionEvidence"
        )
    return value
