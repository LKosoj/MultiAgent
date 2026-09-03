"""Correlation context (run_id/step_name) for LLM provider calls.

Set at the call site around a provider invocation (e.g. in
``workflow/text_to_sql_typed_research.py``) so that
``RetryOpenAIServerModel`` (``retry_openai_model.py``) can attach the
origin of an LLM call to its response log, without threading extra
parameters through every layer between the workflow and the provider.

Not to be confused with ``tool_runtime_context.py``, which carries
workflow-engine metadata for tool calls executed inside a workflow step
boundary. This module is about the narrower LLM-provider-call boundary and
has no dependency on the workflow engine.

Style mirrors ``memory/tools.py::memory_requester_context``.
"""

import contextlib
import contextvars
from typing import Any, Dict, Optional

_LLM_CALL_CONTEXT: contextvars.ContextVar[Dict[str, Any]] = contextvars.ContextVar(
    "llm_call_context",
    default={},
)


@contextlib.contextmanager
def llm_call_context(*, run_id: Optional[str] = None, step_name: Optional[str] = None):
    """Attach run_id/step_name to LLM provider calls made within this block."""
    token = _LLM_CALL_CONTEXT.set({"run_id": run_id, "step_name": step_name})
    try:
        yield
    finally:
        _LLM_CALL_CONTEXT.reset(token)


def get_llm_call_context() -> Dict[str, Any]:
    """Return the current run_id/step_name context (empty dict if unset)."""
    return _LLM_CALL_CONTEXT.get()
