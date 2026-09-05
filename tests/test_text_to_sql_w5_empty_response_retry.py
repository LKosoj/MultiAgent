"""W5: exactly-one-retry on an empty model response.

Covers the three provider-boundary wrappers named in the W5 task:

- workflow/text_to_sql_typed_research.py::_typed_schema_model (via
  ``_research_model``/``_research_stop_review_model``), used for
  schema-research decisions and the research stop-review.
- workflow/text_to_sql_adaptive_solver.py::_production_solver_model, used
  for SQL-solver proposals.
- custom_tools/text_to_sql/adaptive/result_review_runtime.py::review, which
  asks ``utils.call_openai_api`` for one retry instead of zero; tested here
  directly against ``call_openai_api`` (the runtime closure itself needs a
  full solver/candidate fixture out of proportion to what this behaviour
  needs to be pinned).

Both wrappers are always called from *inside* an already-open model-budget
reservation for this exact logical call (``research-model-*``/
``research-stop-review-*`` in research_loop.py, ``solver-generate-*`` in
run_production_adaptive_sql_generation). The ledger only allows one
outstanding reservation per run/incarnation, so every test below reproduces
that by opening one real ``execute_model_call_with_budget_async`` reservation
itself and calling the wrapper's model closure from inside it -- exactly
mirroring the real callers (see tests/test_adaptive_model_budget.py for the
same ``execute_model_call_with_budget_async`` usage pattern this borrows).

Each wrapper is checked for: a non-empty first response never retries; an
empty-then-non-empty response retries exactly once, still inside the single
outer reservation (one ledger record, usage summed across both attempts);
and an empty-then-empty response fails with the same plain ``ValueError`` it
always did -- never a ``BudgetAdmissionError``, which is what a nested
reservation used to raise instead (the W5 review's blocker). The SQL-solver
wrapper additionally skips the retry outright once the deadline is already
exhausted.
"""

from __future__ import annotations

import asyncio

import pytest
from smolagents import ChatMessage, MessageRole

from custom_tools.text_to_sql.adaptive.model_budget import (
    ModelBudgetLimits,
    ModelTokenUsage,
)
from custom_tools.text_to_sql.adaptive.models import SolverStopReason
from custom_tools.text_to_sql.adaptive.policy import (
    MAX_ACTIONS,
    MAX_DB_PROBE_MS,
    MAX_DB_PROBES,
    MAX_INLINE_BYTES,
    MAX_MODEL_CALLS_V2,
    MAX_MODEL_INPUT_TOKENS_PER_CALL,
    MAX_MODEL_OUTPUT_TOKENS_PER_CALL,
    MAX_MODEL_TOTAL_TOKENS,
    MAX_RETURNED_ROWS,
    MAX_SAMPLE_ROWS,
    MAX_WALL_CLOCK_SECONDS,
    AdaptivePolicyConfig,
    BudgetAdmissionError,
    OperationCountBudget,
    PerActionBudget,
    ResourceBudget,
    ResultVolumeBudget,
    WallClockBudget,
    execute_model_call_with_budget_async,
)
from custom_tools.text_to_sql.adaptive.serialization import canonical_digest
from workflow.adaptive_budget_ledger import AdaptiveBudgetLedger
from workflow.deadline import DeadlineBudget


def _config(
    *, input_tokens_per_call: int = MAX_MODEL_INPUT_TOKENS_PER_CALL
) -> AdaptivePolicyConfig:
    limits = ModelBudgetLimits(
        model_calls=MAX_MODEL_CALLS_V2,
        input_tokens_per_call=input_tokens_per_call,
        output_tokens_per_call=MAX_MODEL_OUTPUT_TOKENS_PER_CALL,
        total_tokens=MAX_MODEL_TOTAL_TOKENS,
    )
    return AdaptivePolicyConfig(
        policy_version=2,
        wall_clock=WallClockBudget(wall_clock_seconds=MAX_WALL_CLOCK_SECONDS),
        resource_limits=ResourceBudget(
            model_tokens=limits.total_tokens,
            db_probe_ms=MAX_DB_PROBE_MS,
        ),
        operation_counts=OperationCountBudget(
            actions=MAX_ACTIONS,
            model_decisions=limits.model_calls,
            db_probes=MAX_DB_PROBES,
        ),
        result_volume=ResultVolumeBudget(
            returned_rows=MAX_RETURNED_ROWS,
            inline_bytes=MAX_INLINE_BYTES,
        ),
        per_action=PerActionBudget(sample_rows=MAX_SAMPLE_ROWS),
        model_budget=limits,
    )


class _QueuedProvider:
    """Returns one queued ChatMessage per call; raises if the queue runs dry."""

    def __init__(self, *responses: ChatMessage) -> None:
        self._responses = list(responses)
        self.calls = 0

    def __call__(self, messages, **kwargs):
        self.calls += 1
        if not self._responses:
            raise AssertionError("provider called more times than expected")
        return self._responses.pop(0)


def _msg(content: str, input_tokens: int, output_tokens: int) -> ChatMessage:
    from types import SimpleNamespace

    return ChatMessage(
        role=MessageRole.ASSISTANT,
        content=content,
        token_usage=SimpleNamespace(
            input_tokens=input_tokens, output_tokens=output_tokens
        ),
    )


async def _call_inside_one_reservation(
    ledger,
    policy: AdaptivePolicyConfig,
    run_id: str,
    run_incarnation: str,
    call_id: str,
    model,
    prompt: str = "prompt",
):
    """Call ``model(prompt)`` from inside one real ledger reservation.

    Mirrors exactly how the real callers hold their own reservation open
    while invoking the model closure (research_loop.py's
    ``_ResearchLoopCoordinator``, ``run_production_adaptive_sql_generation``'s
    ``propose``): if the wrapper under test tried to open a second, nested
    reservation for its retry, this outer ``execute_model_call_with_budget_async``
    call would raise ``BudgetConflictError`` before the wrapper's own
    empty-response classification ever ran.
    """

    captured: list[object] = []

    async def _call(_reservation):
        response = await model(prompt)
        captured.append(response)
        return response.usage

    await execute_model_call_with_budget_async(
        run_id,
        run_incarnation,
        call_id,
        canonical_digest({"call_id": call_id}),
        "test-model-identity",
        policy.model_budget.input_tokens_per_call,
        policy.model_budget.output_tokens_per_call,
        _call,
        config=policy,
        ledger=ledger,
    )
    return captured[0]


# ---------------------------------------------------------------------------
# workflow/text_to_sql_typed_research.py::_typed_schema_model
# (via _research_model / _research_stop_review_model)
# ---------------------------------------------------------------------------


def test_typed_research_model_does_not_retry_a_non_empty_response(
    monkeypatch, tmp_path
) -> None:
    import agent_command
    from workflow.text_to_sql_typed_research import _research_model

    provider = _QueuedProvider(_msg('{"ok": true}', 10, 4))
    monkeypatch.setattr(
        agent_command, "create_text_to_sql_model", lambda *a, **k: provider
    )

    ledger = AdaptiveBudgetLedger(tmp_path / "ledger.sqlite")
    try:
        model = _research_model("profile-model", 1024, "run-1")
        response = asyncio.run(
            _call_inside_one_reservation(
                ledger, _config(), "run-1", "incarnation-1", "research-model-0-1", model
            )
        )

        assert response.raw_response == '{"ok": true}'
        assert response.usage == ModelTokenUsage(input_tokens=10, output_tokens=4)
        assert provider.calls == 1

        records = ledger.load_model_records("run-1", "incarnation-1")
        assert len(records) == 1
        assert records[0].reconciliation.charged_input_tokens == 10
        assert records[0].reconciliation.charged_output_tokens == 4
    finally:
        ledger.close()


def test_typed_research_model_retries_once_then_succeeds(
    monkeypatch, tmp_path
) -> None:
    import agent_command
    from workflow.text_to_sql_typed_research import _research_model

    provider = _QueuedProvider(
        _msg("", 11, 1),
        _msg('{"ok": true}', 13, 9),
    )
    monkeypatch.setattr(
        agent_command, "create_text_to_sql_model", lambda *a, **k: provider
    )

    ledger = AdaptiveBudgetLedger(tmp_path / "ledger.sqlite")
    try:
        model = _research_model(
            "profile-model", 1024, "run-2", input_tokens=MAX_MODEL_INPUT_TOKENS_PER_CALL
        )
        response = asyncio.run(
            _call_inside_one_reservation(
                ledger, _config(), "run-2", "incarnation-1", "research-model-0-1", model
            )
        )

        assert provider.calls == 2
        assert response.raw_response == '{"ok": true}'
        # Usage is the *sum* of both attempts, charged once against the one
        # outer reservation -- not the first attempt's usage alone, and not
        # a second reservation's separate charge.
        assert response.usage == ModelTokenUsage(input_tokens=24, output_tokens=10)

        records = ledger.load_model_records("run-2", "incarnation-1")
        assert len(records) == 1
        assert records[0].reconciliation is not None
        assert records[0].reconciliation.charged_input_tokens == 24
        assert records[0].reconciliation.charged_output_tokens == 10
    finally:
        ledger.close()


def test_typed_research_model_retry_usage_is_clamped_to_the_reservation_cap(
    monkeypatch, tmp_path
) -> None:
    """W5 review blocker: summing both attempts' usage without capping it can
    exceed the reservation's own ``maximum_input_tokens`` and make
    ``model_charge`` raise ``ModelUsageBudgetError`` (surfaced as
    ``BudgetReconciliationError``) even though the retry itself produced a
    perfectly good response. Worse, ``_settle_model_result`` durably records
    the ``result`` event *before* that reconcile call
    (custom_tools/text_to_sql/adaptive/_policy_model_budget.py), so a
    resumed replay of the same call_id would keep re-reconciling the same
    stored result and fail forever (a poison-pill call_id).

    Reproduces the exact reported numbers: input_tokens_per_call=15, an
    empty first attempt (11 input tokens) then a successful second attempt
    (13 input tokens) -- the raw sum (24) is well over the cap, but each
    attempt alone is not.
    """
    import agent_command
    from workflow.text_to_sql_typed_research import _research_model

    provider = _QueuedProvider(
        _msg("", 11, 1),
        _msg('{"ok": true}', 13, 9),
    )
    monkeypatch.setattr(
        agent_command, "create_text_to_sql_model", lambda *a, **k: provider
    )

    policy = _config(input_tokens_per_call=15)
    ledger = AdaptiveBudgetLedger(tmp_path / "ledger.sqlite")
    try:
        model = _research_model("profile-model", 1024, "run-cap", input_tokens=15)
        response = asyncio.run(
            _call_inside_one_reservation(
                ledger, policy, "run-cap", "incarnation-1", "research-model-0-1", model
            )
        )

        assert provider.calls == 2
        assert response.raw_response == '{"ok": true}'
        # Clamped to the reservation's own maximum_input_tokens (15), not the
        # raw sum (24); the output field's cap (MAX_MODEL_OUTPUT_TOKENS_PER_CALL)
        # is nowhere near the sum (10), so it stays an unclamped plain sum.
        assert response.usage == ModelTokenUsage(input_tokens=15, output_tokens=10)

        records = ledger.load_model_records("run-cap", "incarnation-1")
        assert len(records) == 1
        assert records[0].reconciliation is not None
        assert records[0].reconciliation.charged_input_tokens == 15
        assert records[0].reconciliation.charged_output_tokens == 10

        # Resume: replaying the exact same call_id (idempotent restart) must
        # return the already-settled reconciliation without re-running the
        # provider or re-raising -- the poison-pill this test guards against.
        async def _must_not_execute(_reservation):
            raise AssertionError("execute must not run again on a settled replay")

        replayed = asyncio.run(
            execute_model_call_with_budget_async(
                "run-cap",
                "incarnation-1",
                "research-model-0-1",
                canonical_digest({"call_id": "research-model-0-1"}),
                "test-model-identity",
                policy.model_budget.input_tokens_per_call,
                policy.model_budget.output_tokens_per_call,
                _must_not_execute,
                config=policy,
                ledger=ledger,
            )
        )
        assert replayed == records[0].reconciliation
    finally:
        ledger.close()


def test_typed_research_model_single_call_over_cap_is_not_clamped(
    monkeypatch, tmp_path
) -> None:
    """Round-3 review: clamping must apply only to a *summed* retry. A single
    (non-retried) call whose provider-reported usage exceeds the cap must keep
    surfacing at reconciliation exactly as before the retry feature existed --
    silently clamping it would hide a genuine budget/estimation anomaly."""
    import agent_command
    from custom_tools.text_to_sql.adaptive._policy_common import BudgetReconciliationError
    from workflow.text_to_sql_typed_research import _research_model

    provider = _QueuedProvider(_msg('{"ok": true}', 20, 4))
    monkeypatch.setattr(
        agent_command, "create_text_to_sql_model", lambda *a, **k: provider
    )

    policy = _config(input_tokens_per_call=15)
    ledger = AdaptiveBudgetLedger(tmp_path / "ledger.sqlite")
    try:
        model = _research_model("profile-model", 1024, "run-single", input_tokens=15)
        with pytest.raises(BudgetReconciliationError):
            asyncio.run(
                _call_inside_one_reservation(
                    ledger, policy, "run-single", "incarnation-1", "research-model-0-1", model
                )
            )
        assert provider.calls == 1
    finally:
        ledger.close()


def test_typed_research_model_raises_after_second_empty_response(
    monkeypatch, tmp_path
) -> None:
    import agent_command
    from workflow.text_to_sql_typed_research import _research_model

    provider = _QueuedProvider(_msg("", 11, 1), _msg("   ", 13, 0))
    monkeypatch.setattr(
        agent_command, "create_text_to_sql_model", lambda *a, **k: provider
    )

    ledger = AdaptiveBudgetLedger(tmp_path / "ledger.sqlite")
    try:
        model = _research_model("profile-model", 1024, "run-3")

        with pytest.raises(ValueError, match="empty") as excinfo:
            asyncio.run(
                _call_inside_one_reservation(
                    ledger,
                    _config(),
                    "run-3",
                    "incarnation-1",
                    "research-model-0-1",
                    model,
                )
            )
        # A plain ValueError, not the BudgetAdmissionError subclass a nested
        # reservation conflict used to raise instead.
        assert not isinstance(excinfo.value, BudgetAdmissionError)
        assert provider.calls == 2

        records = ledger.load_model_records("run-3", "incarnation-1")
        assert len(records) == 1
        assert records[0].reconciliation is not None
    finally:
        ledger.close()


def test_research_stop_review_model_also_retries_with_only_three_positional_args(
    monkeypatch, tmp_path
) -> None:
    """workflow/_text_to_sql_solver_reentry.py calls _research_stop_review_model
    with exactly 3 positional args (no run_incarnation/budget_ledger/policy --
    those kwargs no longer exist), so it never supplies the new keyword-only
    ``input_tokens`` reservation cap either. The retry-once behaviour must
    still apply uniformly: there is no more special-cased "no retry context"
    path. Without ``input_tokens``, this wrapper cannot safely clamp a
    summed retry usage to an unknown cap, so it falls back to charging only
    the successful (second) attempt's own usage -- proven here with a
    deliberately tiny reservation cap (15) that the raw sum (11 + 13 = 24)
    would have overshot, but the second attempt alone (13) fits."""

    import agent_command
    from workflow.text_to_sql_typed_research import _research_stop_review_model

    provider = _QueuedProvider(_msg("", 11, 1), _msg('{"ok": true}', 13, 9))
    monkeypatch.setattr(
        agent_command, "create_text_to_sql_model", lambda *a, **k: provider
    )

    ledger = AdaptiveBudgetLedger(tmp_path / "ledger.sqlite")
    try:
        model = _research_stop_review_model("profile-model", 1024, "run-4")
        response = asyncio.run(
            _call_inside_one_reservation(
                ledger,
                _config(input_tokens_per_call=15),
                "run-4",
                "incarnation-1",
                "research-stop-review-0-1",
                model,
            )
        )

        assert provider.calls == 2
        assert response.raw_response == '{"ok": true}'
        # Only the successful attempt's own usage is charged -- not the sum
        # (24/10), which would have exceeded the 15-token cap.
        assert response.usage == ModelTokenUsage(input_tokens=13, output_tokens=9)

        records = ledger.load_model_records("run-4", "incarnation-1")
        assert len(records) == 1
        assert records[0].reconciliation is not None
        assert records[0].reconciliation.charged_input_tokens == 13
        assert records[0].reconciliation.charged_output_tokens == 9
    finally:
        ledger.close()


# ---------------------------------------------------------------------------
# workflow/text_to_sql_adaptive_solver.py::_production_solver_model
# ---------------------------------------------------------------------------


def _solver_runtime(
    deadline: DeadlineBudget,
    run_id: str = "run-solver",
    *,
    policy: AdaptivePolicyConfig | None = None,
):
    from types import SimpleNamespace

    return SimpleNamespace(
        run_id=run_id,
        verified_research_policy=policy if policy is not None else _config(),
        deadline=deadline,
    )


def test_production_solver_model_does_not_retry_a_non_empty_response(
    monkeypatch, tmp_path
) -> None:
    import agent_command
    from workflow.text_to_sql_adaptive_solver import _production_solver_model

    provider = _QueuedProvider(_msg('{"ok": true}', 10, 4))
    monkeypatch.setattr(
        agent_command, "create_text_to_sql_model", lambda *a, **k: provider
    )

    ledger = AdaptiveBudgetLedger(tmp_path / "ledger.sqlite")
    try:
        runtime = _solver_runtime(DeadlineBudget.from_duration(30))
        model = _production_solver_model(runtime, "profile-model", "system prompt")
        response = asyncio.run(
            _call_inside_one_reservation(
                ledger,
                _config(),
                runtime.run_id,
                "incarnation-1",
                "solver-generate-0-1",
                model,
            )
        )

        assert response.raw_response == '{"ok": true}'
        assert response.usage == ModelTokenUsage(input_tokens=10, output_tokens=4)
        assert provider.calls == 1

        records = ledger.load_model_records(runtime.run_id, "incarnation-1")
        assert len(records) == 1
        assert records[0].reconciliation.charged_input_tokens == 10
        assert records[0].reconciliation.charged_output_tokens == 4
    finally:
        ledger.close()


def test_production_solver_model_retries_once_then_succeeds(
    monkeypatch, tmp_path
) -> None:
    import agent_command
    from workflow.text_to_sql_adaptive_solver import _production_solver_model

    provider = _QueuedProvider(
        _msg("", 11, 1),
        _msg('{"ok": true}', 13, 9),
    )
    monkeypatch.setattr(
        agent_command, "create_text_to_sql_model", lambda *a, **k: provider
    )

    ledger = AdaptiveBudgetLedger(tmp_path / "ledger.sqlite")
    try:
        runtime = _solver_runtime(DeadlineBudget.from_duration(30))
        model = _production_solver_model(runtime, "profile-model", "system prompt")
        response = asyncio.run(
            _call_inside_one_reservation(
                ledger,
                _config(),
                runtime.run_id,
                "incarnation-1",
                "solver-generate-0-1",
                model,
            )
        )

        assert provider.calls == 2
        assert response.raw_response == '{"ok": true}'
        assert response.usage == ModelTokenUsage(input_tokens=24, output_tokens=10)

        records = ledger.load_model_records(runtime.run_id, "incarnation-1")
        assert len(records) == 1
        assert records[0].reconciliation is not None
        assert records[0].reconciliation.charged_input_tokens == 24
        assert records[0].reconciliation.charged_output_tokens == 10
    finally:
        ledger.close()


def test_production_solver_model_retry_usage_is_clamped_to_the_reservation_cap(
    monkeypatch, tmp_path
) -> None:
    """Same W5 review blocker as
    ``test_typed_research_model_retry_usage_is_clamped_to_the_reservation_cap``,
    reproduced against the SQL-solver wrapper: this wrapper always has
    ``runtime.verified_research_policy.model_budget.input_tokens_per_call``
    in scope (the exact value ``run_production_adaptive_sql_generation``'s
    ``propose`` uses for its own ``solver-generate-*`` reservation), so it
    clamps instead of falling back."""

    import agent_command
    from workflow.text_to_sql_adaptive_solver import _production_solver_model

    provider = _QueuedProvider(
        _msg("", 11, 1),
        _msg('{"ok": true}', 13, 9),
    )
    monkeypatch.setattr(
        agent_command, "create_text_to_sql_model", lambda *a, **k: provider
    )

    policy = _config(input_tokens_per_call=15)
    ledger = AdaptiveBudgetLedger(tmp_path / "ledger.sqlite")
    try:
        runtime = _solver_runtime(DeadlineBudget.from_duration(30), policy=policy)
        model = _production_solver_model(runtime, "profile-model", "system prompt")
        response = asyncio.run(
            _call_inside_one_reservation(
                ledger,
                policy,
                runtime.run_id,
                "incarnation-1",
                "solver-generate-0-1",
                model,
            )
        )

        assert provider.calls == 2
        assert response.raw_response == '{"ok": true}'
        assert response.usage == ModelTokenUsage(input_tokens=15, output_tokens=10)

        records = ledger.load_model_records(runtime.run_id, "incarnation-1")
        assert len(records) == 1
        assert records[0].reconciliation is not None
        assert records[0].reconciliation.charged_input_tokens == 15
        assert records[0].reconciliation.charged_output_tokens == 10

        # Resume: replaying the exact same call_id must return the
        # already-settled reconciliation without re-running the provider or
        # re-raising -- the poison-pill this test guards against.
        async def _must_not_execute(_reservation):
            raise AssertionError("execute must not run again on a settled replay")

        replayed = asyncio.run(
            execute_model_call_with_budget_async(
                runtime.run_id,
                "incarnation-1",
                "solver-generate-0-1",
                canonical_digest({"call_id": "solver-generate-0-1"}),
                "test-model-identity",
                policy.model_budget.input_tokens_per_call,
                policy.model_budget.output_tokens_per_call,
                _must_not_execute,
                config=policy,
                ledger=ledger,
            )
        )
        assert replayed == records[0].reconciliation
    finally:
        ledger.close()


def test_production_solver_model_raises_after_second_empty_response(
    monkeypatch, tmp_path
) -> None:
    import agent_command
    from workflow.text_to_sql_adaptive_solver import (
        _production_solver_model,
        _proposal_failure_reason,
    )

    provider = _QueuedProvider(_msg("", 11, 1), _msg("   ", 13, 0))
    monkeypatch.setattr(
        agent_command, "create_text_to_sql_model", lambda *a, **k: provider
    )

    ledger = AdaptiveBudgetLedger(tmp_path / "ledger.sqlite")
    try:
        runtime = _solver_runtime(DeadlineBudget.from_duration(30))
        model = _production_solver_model(runtime, "profile-model", "system prompt")

        with pytest.raises(ValueError, match="empty") as excinfo:
            asyncio.run(
                _call_inside_one_reservation(
                    ledger,
                    _config(),
                    runtime.run_id,
                    "incarnation-1",
                    "solver-generate-0-1",
                    model,
                )
            )
        assert not isinstance(excinfo.value, BudgetAdmissionError)
        # The exact classification the solver's own failure-reason mapping
        # gives this error: TOOL_FAILURE, never BUDGET_EXHAUSTED (which is
        # what a nested reservation's BudgetConflictError used to cause).
        assert _proposal_failure_reason(excinfo.value) is SolverStopReason.TOOL_FAILURE
        assert provider.calls == 2

        records = ledger.load_model_records(runtime.run_id, "incarnation-1")
        assert len(records) == 1
        assert records[0].reconciliation is not None
    finally:
        ledger.close()


def test_production_solver_model_skips_retry_when_deadline_is_exhausted(
    monkeypatch, tmp_path
) -> None:
    import agent_command
    from workflow.text_to_sql_adaptive_solver import _production_solver_model

    provider = _QueuedProvider(_msg("", 11, 1))
    monkeypatch.setattr(
        agent_command, "create_text_to_sql_model", lambda *a, **k: provider
    )

    ledger = AdaptiveBudgetLedger(tmp_path / "ledger.sqlite")
    try:
        exhausted_deadline = DeadlineBudget(
            deadline_monotonic=0.0,
            deadline_at_ms=0,
            monotonic=lambda: 1.0,
            wall_time=lambda: 1.0,
        )
        runtime = _solver_runtime(exhausted_deadline)
        model = _production_solver_model(runtime, "profile-model", "system prompt")

        with pytest.raises(ValueError, match="empty"):
            asyncio.run(
                _call_inside_one_reservation(
                    ledger,
                    _config(),
                    runtime.run_id,
                    "incarnation-1",
                    "solver-generate-0-1",
                    model,
                )
            )
        # No retry attempted: the provider was called exactly once even
        # though its one response was empty.
        assert provider.calls == 1
    finally:
        ledger.close()


# ---------------------------------------------------------------------------
# custom_tools/text_to_sql/adaptive/result_review_runtime.py::review
#
# review() itself only changed max_retries=0 -> 1 on its call_openai_api(...)
# call; building a real runtime for build_result_review_runtime needs a full
# solver/candidate/schema fixture unrelated to this behaviour, so this pins
# the exact call_openai_api semantics review() now relies on instead.
# ---------------------------------------------------------------------------


def test_call_openai_api_max_retries_one_retries_once_on_empty_then_succeeds() -> None:
    from utils import call_openai_api

    provider = _QueuedProvider(_msg("", 5, 0), _msg('{"verdict": "ok"}', 6, 2))

    response = call_openai_api(
        prompt="review prompt",
        model=provider,
        max_tokens=256,
        max_retries=1,
        response_format={"type": "json_schema", "json_schema": {"name": "x"}},
    )

    assert response == '{"verdict": "ok"}'
    assert provider.calls == 2


def test_call_openai_api_max_retries_one_fails_after_second_empty_response() -> None:
    """call_openai_api's public contract on persistent failure is (and stays)
    to return "" rather than raise -- see utils.py's final ``return ""``.
    review() has no separate emptiness check of its own, so this "" flows
    through unchanged to the caller, exactly as it did before this change
    (which only adds the one retry attempt in between)."""

    from utils import call_openai_api

    provider = _QueuedProvider(_msg("", 5, 0), _msg("   ", 5, 0))

    response = call_openai_api(
        prompt="review prompt",
        model=provider,
        max_tokens=256,
        max_retries=1,
        response_format={"type": "json_schema", "json_schema": {"name": "x"}},
    )
    assert response == ""
    assert provider.calls == 2


def test_call_openai_api_max_retries_one_does_not_retry_a_non_empty_response() -> None:
    from utils import call_openai_api

    provider = _QueuedProvider(_msg('{"verdict": "ok"}', 6, 2))

    response = call_openai_api(
        prompt="review prompt",
        model=provider,
        max_tokens=256,
        max_retries=1,
        response_format={"type": "json_schema", "json_schema": {"name": "x"}},
    )

    assert response == '{"verdict": "ok"}'
    assert provider.calls == 1
