"""Shared retry-once-on-empty-response helper for text-to-sql model wrappers.

Both the schema-research/stop-review model wrapper
(``workflow/text_to_sql_typed_research.py::_typed_schema_model``) and the
SQL-solver model wrapper
(``workflow/text_to_sql_adaptive_solver.py::_production_solver_model``) call
an LLM provider that may return an empty response for one call. Both treat
that the same way: log a WARNING, call the provider once more with the exact
same prompt, and classify the result on the *second* attempt's response
(empty or not) -- a persistently empty response after the retry fails
exactly like it did before the retry was added.

Both wrappers are themselves invoked from *inside* an already-open model
budget-ledger reservation for this logical call (``research-model-*`` in
``custom_tools/text_to_sql/adaptive/research_loop.py``, ``solver-generate-*``
in ``workflow/text_to_sql_adaptive_solver.py``). A retry must stay a second
*provider* call within that one reservation, not open a second, nested
reservation of its own: the ledger only allows one outstanding reservation
per run/incarnation
(``custom_tools/text_to_sql/adaptive/_policy_model_budget.py``,
``workflow/adaptive_budget_ledger.py``), so a nested reservation always fails
with ``BudgetConflictError`` (a ``BudgetAdmissionError`` subclass), which
misclassifies a plain empty-response failure as budget exhaustion instead of
a protocol/tool failure.

``custom_tools/text_to_sql/adaptive/result_review_runtime.py::review`` has no
ledger reservation to protect (it is not on the durable model-budget
ledger), so it instead asks ``utils.call_openai_api``'s own built-in
``max_retries=1`` for the same one-retry behaviour; see the comment there.

A retry is a *second provider call inside the one reservation*, so its usage
must still fit under that reservation's per-call maxima
(``ModelCallReservation.maximum_input_tokens``/``maximum_output_tokens`` in
``custom_tools/text_to_sql/adaptive/model_budget.py``) even though it is now
the sum of two attempts instead of one. ``sum_model_token_usage`` below
clamps the sum to those maxima when a caller supplies them, so a wasted
empty first attempt can never push the summed usage over the reservation's
own cap and make ``model_charge`` raise ``ModelUsageBudgetError`` (which
``reconcile_model_call_usage`` turns into ``BudgetReconciliationError``).
That matters beyond the immediate exception: ``_settle_model_result`` in
``custom_tools/text_to_sql/adaptive/_policy_model_budget.py`` durably records
the ``result`` event *before* reconciling it, so a reconcile failure there
cannot be recovered by simply retrying -- resume re-reads the same durable
``result`` and reconciles it again, failing forever (a poison-pill call_id).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

from smolagents import ChatMessage

from custom_tools.text_to_sql.adaptive.model_budget import ModelTokenUsage

_T = TypeVar("_T")

logger = logging.getLogger(__name__)


def _raw_response_text(response: object) -> object:
    """Extract the raw text/bytes payload from a provider response, if any.

    Shared by every model wrapper's emptiness check: a ``str``/``bytes``
    response (or a ``ChatMessage`` wrapping one) yields that payload;
    anything else (e.g. a malformed shape the provider returned) yields
    ``None``, which ``_is_empty``/``_is_blank_text`` both treat as "not a
    retryable blank string".
    """

    if type(response) in (bytes, str):
        return response
    if isinstance(response, ChatMessage):
        return response.content
    return None


def _is_empty(raw_response: object) -> bool:
    """Final emptiness check callers raise on: wrong type, or blank text."""

    return type(raw_response) not in (bytes, str) or not raw_response.strip()


def _is_blank_text(raw_response: object) -> bool:
    """Whether ``raw_response`` is worth retrying (a genuinely blank string).

    Only a genuinely blank *valid-type* response (empty/whitespace str or
    bytes) is worth retrying: an identical retry cannot fix a malformed
    response shape (e.g. the provider returning a raw dict instead of
    chat-completion text), so that case skips the retry and goes straight to
    the unchanged ``_is_empty`` classification.
    """

    return type(raw_response) in (bytes, str) and not raw_response.strip()


async def retry_once_on_empty_response(
    call: Callable[[], Awaitable[_T]],
    *,
    is_empty: Callable[[_T], bool],
    log_context: str,
) -> tuple[_T, tuple[_T, ...]]:
    """Call ``call()``; if the response is empty, retry it exactly once.

    Returns ``(final_response, attempts)``: ``final_response`` is the last
    response produced (the retry's, if a retry happened) -- callers classify
    emptiness on this value, unchanged from before this helper existed.
    ``attempts`` holds every response produced, in call order (length 1 if no
    retry was needed, length 2 otherwise), so callers can fold usage across
    both tries into the one outstanding reservation instead of charging a
    second one.
    """

    response = await call()
    if not is_empty(response):
        return response, (response,)
    logger.warning("%s: model response was empty; retrying once", log_context)
    retry_response = await call()
    return retry_response, (response, retry_response)


def sum_model_token_usage(
    usages: tuple[ModelTokenUsage, ...],
    *,
    max_input_tokens: int | None = None,
    max_output_tokens: int | None = None,
) -> ModelTokenUsage:
    """Sum provider token usage across every attempt in ``usages``.

    A field stays ``None`` (unknown) if any attempt reported ``None`` for
    it, so the ledger's own reconciliation keeps charging its conservative
    maximum instead of an understated sum (see
    ``reconcile_model_call_usage`` in
    ``custom_tools/text_to_sql/adaptive/_policy_model_budget.py``).

    ``max_input_tokens``/``max_output_tokens`` are the *same* per-call caps
    the caller used to open its one outstanding reservation for this call
    (``ModelCallReservation.maximum_input_tokens``/``maximum_output_tokens``,
    i.e. the policy's ``model_budget.input_tokens_per_call``/
    ``output_tokens_per_call``). When given, and the summed value for that
    field would exceed its cap, the sum is clamped down to the cap instead:
    charging the reservation's own maximum still correctly reserves
    "unknown, charge conservatively" semantics, it just also caps a *known*
    sum that would otherwise overshoot and make ``model_charge`` raise
    ``ModelUsageBudgetError``. This is a deliberate undercount of the wasted
    empty attempt's own tokens (they are not fully charged) traded for an
    always-reconcilable reservation -- see the module docstring.

    Passing ``None`` for either cap (the default) leaves that field's sum
    unclamped, exactly as before this parameter existed.
    """

    def _sum_field(name: str) -> int | None:
        values = [getattr(usage, name) for usage in usages]
        if any(value is None for value in values):
            return None
        return sum(values)

    def _clamp_field(name: str, total: int | None, cap: int | None) -> int | None:
        if total is None or cap is None or total <= cap:
            return total
        logger.warning(
            "usage summed across %d provider attempts exceeded the "
            "reservation cap; clamped (field=%s summed=%d cap=%d)",
            len(usages),
            name,
            total,
            cap,
        )
        return cap

    return ModelTokenUsage(
        input_tokens=_clamp_field(
            "input_tokens", _sum_field("input_tokens"), max_input_tokens
        ),
        output_tokens=_clamp_field(
            "output_tokens", _sum_field("output_tokens"), max_output_tokens
        ),
    )


__all__ = [
    "retry_once_on_empty_response",
    "sum_model_token_usage",
    "_raw_response_text",
    "_is_empty",
    "_is_blank_text",
]
