"""W3-04 tests for v2 model-call budget facts in the adaptive ledger."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import multiprocessing
import sqlite3
import threading

import pytest
from pydantic import ValidationError

from custom_tools.text_to_sql.adaptive.model_budget import (
    SQLITE_SIGNED_INTEGER_MAX,
    ModelBudgetLimits,
    ModelCallReservation,
    ModelCallResult,
    ModelCallStarted,
    ModelTokenUsage,
)
from custom_tools.text_to_sql.adaptive.models import (
    EvidenceCost,
    ResearchActionKind,
    TableRef,
)
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
    BudgetConflictError,
    BudgetExhaustedError,
    BudgetReconciliationError,
    BudgetReservation,
    OperationCountBudget,
    PerActionBudget,
    ResourceBudget,
    ResultVolumeBudget,
    WallClockBudget,
    completed_model_budget_chain,
    execute_model_call_with_budget,
    execute_model_call_with_budget_async,
    initial_budget_state,
    initial_model_budget_state,
    reconcile_model_call_usage,
    reserve_model_call_budget,
)
from custom_tools.text_to_sql.adaptive.serialization import canonical_digest
from custom_tools.text_to_sql.adaptive.serialization import (
    canonical_json_bytes,
    deserialize_as,
)
from workflow.adaptive_budget_ledger import (
    ADAPTIVE_BUDGET_SCHEMA_VERSION,
    EXECUTION_CLAIM_LEASE_NS,
    AdaptiveBudgetLedger,
    _MODEL_SCHEMA_SQL,
    _V1_SCHEMA_SQL,
)


RUN_ID = "model-run"
INCARNATION = "model-incarnation"
MODEL_IDENTITY = "provider/model-v1"


def _request_digest(label: str = "request") -> str:
    return canonical_digest({"request": label})


def _config(
    *, policy_version: int = 2, wall_clock_seconds: int = MAX_WALL_CLOCK_SECONDS
) -> AdaptivePolicyConfig:
    limits = ModelBudgetLimits(
        model_calls=MAX_MODEL_CALLS_V2,
        input_tokens_per_call=MAX_MODEL_INPUT_TOKENS_PER_CALL,
        output_tokens_per_call=MAX_MODEL_OUTPUT_TOKENS_PER_CALL,
        total_tokens=MAX_MODEL_TOTAL_TOKENS,
    )
    return AdaptivePolicyConfig(
        policy_version=policy_version,
        wall_clock=WallClockBudget(wall_clock_seconds=wall_clock_seconds),
        resource_limits=ResourceBudget(
            model_tokens=limits.total_tokens if policy_version == 2 else 0,
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
        model_budget=limits if policy_version == 2 else None,
    )


def _model_result(
    reservation: ModelCallReservation,
    invocation_id: str,
    usage: ModelTokenUsage,
    claim_generation: int = 0,
    started_claim_generation: int | None = None,
) -> ModelCallResult:
    if started_claim_generation is None:
        started_claim_generation = claim_generation
    values = {
        "reservation": reservation,
        "invocation_id": invocation_id,
        "started_claim_generation": started_claim_generation,
        "claim_generation": claim_generation,
        "usage": usage,
    }
    return ModelCallResult(**values, result_digest=canonical_digest(values))


class _SynchronizedClaimLedger(AdaptiveBudgetLedger):
    def __init__(self, db_path, barrier: threading.Barrier) -> None:
        self._claim_sync_lock = threading.Lock()
        self._claim_barrier = barrier
        self._claim_sync_remaining = 2
        super().__init__(db_path)

    def synchronize_next_claims(self, barrier: threading.Barrier) -> None:
        with self._claim_sync_lock:
            self._claim_barrier = barrier
            self._claim_sync_remaining = 2

    def claim_model_execution(self, *args, **kwargs):
        claim = super().claim_model_execution(*args, **kwargs)
        with self._claim_sync_lock:
            synchronize = self._claim_sync_remaining > 0
            if synchronize:
                self._claim_sync_remaining -= 1
            barrier = self._claim_barrier
        if synchronize:
            barrier.wait(timeout=5)
        return claim


def _claim_model_in_process(path, reservation_json, barrier, results) -> None:
    ledger = AdaptiveBudgetLedger(path)
    try:
        reservation = ModelCallReservation.model_validate_json(reservation_json)
        barrier.wait(timeout=5)
        claim = ledger.claim_model_execution(reservation, "shared-owner", now_ns=1)
        results.put((claim.acquired, claim.generation))
    finally:
        ledger.close()


def test_v1_model_budget_stays_disabled_and_v2_defaults_are_explicit(tmp_path) -> None:
    ledger = AdaptiveBudgetLedger(tmp_path / "v1.sqlite")
    try:
        with pytest.raises(BudgetExhaustedError, match="policy v2"):
            reserve_model_call_budget(
                RUN_ID,
                INCARNATION,
                "model-1",
                _request_digest(),
                MODEL_IDENTITY,
                1,
                1,
                config=_config(policy_version=1),
                ledger=ledger,
            )
        state = initial_model_budget_state(_config())
        assert state.initial_total_tokens == 524_288
        assert state.initial_input_tokens == 256 * 16_384
        assert MAX_MODEL_OUTPUT_TOKENS_PER_CALL == 32_000
        assert state.initial_output_tokens == 256 * 32_000
    finally:
        ledger.close()


def test_reported_usage_is_charged_once_across_duplicate_execution(tmp_path) -> None:
    ledger = AdaptiveBudgetLedger(tmp_path / "exact.sqlite")
    calls = 0

    def execute(_reservation):
        nonlocal calls
        calls += 1
        return ModelTokenUsage(input_tokens=101, output_tokens=23)

    try:
        first = execute_model_call_with_budget(
            RUN_ID,
            INCARNATION,
            "model-1",
            _request_digest(),
            MODEL_IDENTITY,
            200,
            100,
            execute,
            config=_config(),
            ledger=ledger,
            claim_now_ns=lambda: 1,
            owner_token_factory=lambda: "owner-1",
        )
        second = execute_model_call_with_budget(
            RUN_ID,
            INCARNATION,
            "model-1",
            _request_digest(),
            MODEL_IDENTITY,
            200,
            100,
            execute,
            config=_config(),
            ledger=ledger,
            claim_now_ns=lambda: 2,
            owner_token_factory=lambda: "owner-2",
        )

        assert calls == 1
        assert first == second
        assert first.charged_input_tokens == 101
        assert first.charged_output_tokens == 23
        assert first.charged_total_tokens == 124
        assert first.usage_was_conservative is False
        record = ledger.load_model_records(RUN_ID, INCARNATION)[0]
        assert record.started is not None
        assert record.reconciliation == first
    finally:
        ledger.close()


def test_completed_model_chain_rejects_another_policy_with_equal_limits(
    tmp_path,
) -> None:
    ledger = AdaptiveBudgetLedger(tmp_path / "policy-anchor.sqlite")
    policy = _config()
    try:
        execute_model_call_with_budget(
            RUN_ID,
            INCARNATION,
            "policy-anchor",
            _request_digest("policy-anchor"),
            MODEL_IDENTITY,
            200,
            100,
            lambda _reservation: ModelTokenUsage(input_tokens=10, output_tokens=5),
            config=policy,
            ledger=ledger,
            claim_now_ns=lambda: 1,
            owner_token_factory=lambda: "policy-anchor-owner",
        )

        with pytest.raises(BudgetAdmissionError, match="policy does not match"):
            completed_model_budget_chain(
                ledger.load_model_records(RUN_ID, INCARNATION),
                config=_config(wall_clock_seconds=MAX_WALL_CLOCK_SECONDS - 1),
            )
    finally:
        ledger.close()


@pytest.mark.parametrize("asynchronous", (False, True))
def test_model_reservation_rejects_mixed_policy_before_provider_or_write(
    tmp_path, asynchronous: bool
) -> None:
    ledger = AdaptiveBudgetLedger(tmp_path / "mixed-policy.sqlite")
    first_policy = _config(wall_clock_seconds=MAX_WALL_CLOCK_SECONDS - 1)
    active_policy = _config()
    calls = 0
    try:
        execute_model_call_with_budget(
            RUN_ID,
            INCARNATION,
            "old-policy-call",
            _request_digest("old-policy-call"),
            MODEL_IDENTITY,
            200,
            100,
            lambda _reservation: ModelTokenUsage(input_tokens=10, output_tokens=5),
            config=first_policy,
            ledger=ledger,
            claim_now_ns=lambda: 1,
            owner_token_factory=lambda: "old-policy-owner",
        )

        def provider(_reservation):
            nonlocal calls
            calls += 1
            return ModelTokenUsage(input_tokens=1, output_tokens=1)

        async def async_provider(_reservation):
            nonlocal calls
            calls += 1
            return ModelTokenUsage(input_tokens=1, output_tokens=1)

        if asynchronous:

            async def run() -> None:
                with pytest.raises(BudgetAdmissionError, match="policy does not match"):
                    await execute_model_call_with_budget_async(
                        RUN_ID,
                        INCARNATION,
                        "active-policy-call",
                        _request_digest("active-policy-call"),
                        MODEL_IDENTITY,
                        200,
                        100,
                        async_provider,
                        config=active_policy,
                        ledger=ledger,
                    )

            asyncio.run(run())
        else:
            with pytest.raises(BudgetAdmissionError, match="policy does not match"):
                execute_model_call_with_budget(
                    RUN_ID,
                    INCARNATION,
                    "active-policy-call",
                    _request_digest("active-policy-call"),
                    MODEL_IDENTITY,
                    200,
                    100,
                    provider,
                    config=active_policy,
                    ledger=ledger,
                )

        assert calls == 0
        assert [
            record.reservation.call_id
            for record in ledger.load_model_records(RUN_ID, INCARNATION)
        ] == ["old-policy-call"]
    finally:
        ledger.close()


def test_missing_usage_charges_reserved_maximums(tmp_path) -> None:
    ledger = AdaptiveBudgetLedger(tmp_path / "missing.sqlite")
    try:
        reconciliation = execute_model_call_with_budget(
            RUN_ID,
            INCARNATION,
            "model-unknown-usage",
            _request_digest("unknown"),
            MODEL_IDENTITY,
            200,
            100,
            lambda _reservation: ModelTokenUsage(input_tokens=None, output_tokens=None),
            config=_config(),
            ledger=ledger,
            claim_now_ns=lambda: 1,
            owner_token_factory=lambda: "owner",
        )
        assert reconciliation.charged_input_tokens == 200
        assert reconciliation.charged_output_tokens == 100
        assert reconciliation.charged_total_tokens == 300
        assert reconciliation.usage_was_conservative is True
    finally:
        ledger.close()


@pytest.mark.parametrize(
    ("request_digest", "model_identity", "maximum_input", "maximum_output"),
    [
        (_request_digest("different"), MODEL_IDENTITY, 200, 100),
        (_request_digest(), "provider/model-v2", 200, 100),
        (_request_digest(), MODEL_IDENTITY, 201, 100),
        (_request_digest(), MODEL_IDENTITY, 200, 101),
    ],
)
def test_call_id_replay_requires_exact_request_model_policy_and_maxima(
    tmp_path,
    request_digest,
    model_identity,
    maximum_input,
    maximum_output,
) -> None:
    ledger = AdaptiveBudgetLedger(tmp_path / "identity.sqlite")
    calls = 0

    def execute(_reservation):
        nonlocal calls
        calls += 1
        return ModelTokenUsage(input_tokens=10, output_tokens=5)

    try:
        execute_model_call_with_budget(
            RUN_ID,
            INCARNATION,
            "stable-call",
            _request_digest(),
            MODEL_IDENTITY,
            200,
            100,
            execute,
            config=_config(),
            ledger=ledger,
            claim_now_ns=lambda: 1,
            owner_token_factory=lambda: "first-owner",
        )
        with pytest.raises(BudgetConflictError, match="identity|reservation"):
            execute_model_call_with_budget(
                RUN_ID,
                INCARNATION,
                "stable-call",
                request_digest,
                model_identity,
                maximum_input,
                maximum_output,
                execute,
                config=_config(),
                ledger=ledger,
                claim_now_ns=lambda: 2,
                owner_token_factory=lambda: "second-owner",
            )
        assert calls == 1
    finally:
        ledger.close()


def test_model_reservation_rejects_request_digest_and_identity_spoof(tmp_path) -> None:
    ledger = AdaptiveBudgetLedger(tmp_path / "spoof.sqlite")
    try:
        reservation = reserve_model_call_budget(
            RUN_ID,
            INCARNATION,
            "spoof-call",
            _request_digest("spoof"),
            MODEL_IDENTITY,
            200,
            100,
            config=_config(),
            ledger=ledger,
        )
        values = reservation.model_dump(mode="python")
        values["request_digest"] = "sha1:" + "0" * 40
        with pytest.raises(ValidationError, match="request_digest"):
            ModelCallReservation.model_validate(values)

        values = reservation.model_dump(mode="python")
        values["request_digest"] = _request_digest("forged")
        with pytest.raises(ValidationError, match="reservation_digest"):
            ModelCallReservation.model_validate(values)

        values = reservation.model_dump(mode="python")
        values["model_identity"] = ""
        with pytest.raises(ValidationError, match="model_identity"):
            ModelCallReservation.model_validate(values)
    finally:
        ledger.close()


def test_call_id_replay_rejects_changed_policy_before_provider(tmp_path) -> None:
    ledger = AdaptiveBudgetLedger(tmp_path / "policy-identity.sqlite")
    calls = 0

    def execute(_reservation):
        nonlocal calls
        calls += 1
        return ModelTokenUsage(input_tokens=10, output_tokens=5)

    base = _config()
    changed = AdaptivePolicyConfig(
        **{
            **base.model_dump(mode="python"),
            "resource_limits": ResourceBudget(
                model_tokens=32_768,
                db_probe_ms=MAX_DB_PROBE_MS,
            ),
            "model_budget": ModelBudgetLimits(
                model_calls=MAX_MODEL_CALLS_V2,
                input_tokens_per_call=MAX_MODEL_INPUT_TOKENS_PER_CALL,
                output_tokens_per_call=MAX_MODEL_OUTPUT_TOKENS_PER_CALL,
                total_tokens=32_768,
            ),
        }
    )
    try:
        execute_model_call_with_budget(
            RUN_ID,
            INCARNATION,
            "policy-call",
            _request_digest("policy"),
            MODEL_IDENTITY,
            200,
            100,
            execute,
            config=base,
            ledger=ledger,
            claim_now_ns=lambda: 1,
            owner_token_factory=lambda: "first-owner",
        )
        with pytest.raises(BudgetConflictError, match="identity|reservation"):
            execute_model_call_with_budget(
                RUN_ID,
                INCARNATION,
                "policy-call",
                _request_digest("policy"),
                MODEL_IDENTITY,
                200,
                100,
                execute,
                config=changed,
                ledger=ledger,
            )
        assert calls == 1
    finally:
        ledger.close()


def test_invalid_request_digest_stops_before_reservation_and_provider(tmp_path) -> None:
    ledger = AdaptiveBudgetLedger(tmp_path / "before-reserve.sqlite")
    invoked = False

    def execute(_reservation):
        nonlocal invoked
        invoked = True
        return ModelTokenUsage(input_tokens=1, output_tokens=1)

    try:
        with pytest.raises(BudgetAdmissionError, match="request_digest"):
            execute_model_call_with_budget(
                RUN_ID,
                INCARNATION,
                "before-reserve",
                "sha256:not-hex",
                MODEL_IDENTITY,
                200,
                100,
                execute,
                config=_config(),
                ledger=ledger,
            )
        assert invoked is False
        assert ledger.load_model_records(RUN_ID, INCARNATION) == ()
    finally:
        ledger.close()


@pytest.mark.parametrize(
    "usage",
    [
        ModelTokenUsage(input_tokens=201, output_tokens=1),
        ModelTokenUsage(input_tokens=1, output_tokens=101),
        ModelTokenUsage(input_tokens=10**9, output_tokens=10**9),
    ],
)
def test_provider_usage_above_reservation_fails_closed_and_blocks_replay(
    tmp_path,
    usage,
) -> None:
    ledger = AdaptiveBudgetLedger(tmp_path / "overage.sqlite")
    calls = 0

    def execute(_reservation):
        nonlocal calls
        calls += 1
        return usage

    try:
        with pytest.raises(BudgetReconciliationError, match="reserved maximum"):
            execute_model_call_with_budget(
                RUN_ID,
                INCARNATION,
                "over-call",
                _request_digest("over"),
                MODEL_IDENTITY,
                200,
                100,
                execute,
                config=_config(),
                ledger=ledger,
                claim_now_ns=lambda: 1,
                owner_token_factory=lambda: "owner",
            )
        record = ledger.load_model_records(RUN_ID, INCARNATION)[0]
        assert record.result is not None
        assert record.reconciliation is None

        with pytest.raises(BudgetReconciliationError, match="reserved maximum"):
            execute_model_call_with_budget(
                RUN_ID,
                INCARNATION,
                "over-call",
                _request_digest("over"),
                MODEL_IDENTITY,
                200,
                100,
                execute,
                config=_config(),
                ledger=ledger,
                claim_now_ns=lambda: 2,
                owner_token_factory=lambda: "replay-owner",
            )
        with pytest.raises(BudgetConflictError, match="outstanding"):
            reserve_model_call_budget(
                RUN_ID,
                INCARNATION,
                "next-call",
                _request_digest("next"),
                MODEL_IDENTITY,
                1,
                1,
                config=_config(),
                ledger=ledger,
            )
        assert calls == 1
    finally:
        ledger.close()


@pytest.mark.parametrize("_repeat", range(20))
def test_reservation_rejects_maxima_above_remaining_total_before_provider(
    tmp_path, _repeat: int
) -> None:
    base = _config()
    config = AdaptivePolicyConfig(
        **{
            **base.model_dump(mode="python"),
            "resource_limits": ResourceBudget(
                model_tokens=250,
                db_probe_ms=MAX_DB_PROBE_MS,
            ),
            "model_budget": ModelBudgetLimits(
                model_calls=MAX_MODEL_CALLS_V2,
                input_tokens_per_call=200,
                output_tokens_per_call=100,
                total_tokens=250,
            ),
        }
    )
    ledger = AdaptiveBudgetLedger(tmp_path / "remaining-total.sqlite")
    invoked = False

    def execute(_reservation):
        nonlocal invoked
        invoked = True
        return ModelTokenUsage(input_tokens=1, output_tokens=1)

    try:
        with pytest.raises(BudgetExhaustedError, match="total token"):
            execute_model_call_with_budget(
                RUN_ID,
                INCARNATION,
                "too-large",
                _request_digest("too-large"),
                MODEL_IDENTITY,
                200,
                100,
                execute,
                config=config,
                ledger=ledger,
            )
        assert invoked is False
        assert ledger.load_model_records(RUN_ID, INCARNATION) == ()
    finally:
        ledger.close()


def test_model_budget_dto_round_trips_through_additive_serialization(tmp_path) -> None:
    ledger = AdaptiveBudgetLedger(tmp_path / "serialization.sqlite")
    try:
        reconciliation = execute_model_call_with_budget(
            RUN_ID,
            INCARNATION,
            "model-serialized",
            _request_digest("serialized"),
            MODEL_IDENTITY,
            200,
            100,
            lambda _reservation: ModelTokenUsage(input_tokens=10, output_tokens=5),
            config=_config(),
            ledger=ledger,
            claim_now_ns=lambda: 1,
            owner_token_factory=lambda: "serialization-owner",
        )
        record = ledger.load_model_records(RUN_ID, INCARNATION)[0]
        for model in (
            _config().model_budget,
            record.reservation.budget_before,
            record.result.usage if record.result is not None else None,
            record.reservation,
            record.started,
            record.result,
            reconciliation,
            record,
        ):
            assert model is not None
            assert deserialize_as(canonical_json_bytes(model), type(model)) == model
    finally:
        ledger.close()


def test_started_model_call_recovers_without_repeating_the_provider_call(
    tmp_path,
) -> None:
    path = tmp_path / "replay.sqlite"
    first = AdaptiveBudgetLedger(path)
    reservation = reserve_model_call_budget(
        RUN_ID,
        INCARNATION,
        "model-crash",
        _request_digest("crash"),
        MODEL_IDENTITY,
        200,
        100,
        config=_config(),
        ledger=first,
    )
    claim = first.claim_model_execution(reservation, "lost-owner", now_ns=1)
    assert claim.acquired is True
    assert claim.generation == 0
    started_values = {
        "reservation": reservation,
        "invocation_id": "provider-call-1",
        "claim_generation": claim.generation,
        "started_at_ns": 2,
    }
    first.record_model_started(
        ModelCallStarted(
            **started_values,
            started_digest=canonical_digest(started_values),
        ),
        owner_token="lost-owner",
    )
    first.close()

    restarted = AdaptiveBudgetLedger(path)
    invoked = False

    def must_not_execute(_reservation):
        nonlocal invoked
        invoked = True
        return ModelTokenUsage(input_tokens=1, output_tokens=1)

    try:
        reconciliation = execute_model_call_with_budget(
            RUN_ID,
            INCARNATION,
            "model-crash",
            _request_digest("crash"),
            MODEL_IDENTITY,
            200,
            100,
            must_not_execute,
            config=_config(),
            ledger=restarted,
            claim_now_ns=lambda: 1 + EXECUTION_CLAIM_LEASE_NS,
            owner_token_factory=lambda: "recovery-owner",
        )
        assert invoked is False
        assert reconciliation.usage_was_conservative is True
        assert reconciliation.charged_total_tokens == 300
    finally:
        restarted.close()


def test_provider_failure_is_conservatively_settled_without_replay(tmp_path) -> None:
    ledger = AdaptiveBudgetLedger(tmp_path / "provider-failure.sqlite")
    calls = 0

    def fail(_reservation):
        nonlocal calls
        calls += 1
        raise RuntimeError("provider connection was lost")

    try:
        with pytest.raises(RuntimeError, match="connection was lost"):
            execute_model_call_with_budget(
                RUN_ID,
                INCARNATION,
                "provider-failure",
                _request_digest("provider-failure"),
                MODEL_IDENTITY,
                200,
                100,
                fail,
                config=_config(),
                ledger=ledger,
                claim_now_ns=lambda: 1,
                owner_token_factory=lambda: "failed-owner",
            )
        record = ledger.load_model_records(RUN_ID, INCARNATION)[0]
        assert record.started is not None
        assert record.result is not None
        assert record.reconciliation is not None
        assert record.reconciliation.usage_was_conservative is True
        reconciliation = execute_model_call_with_budget(
            RUN_ID,
            INCARNATION,
            "provider-failure",
            _request_digest("provider-failure"),
            MODEL_IDENTITY,
            200,
            100,
            fail,
            config=_config(),
            ledger=ledger,
            claim_now_ns=lambda: 1 + EXECUTION_CLAIM_LEASE_NS,
            owner_token_factory=lambda: "recovery-owner",
        )
        assert calls == 1
        assert reconciliation.usage_was_conservative is True
        record = ledger.load_model_records(RUN_ID, INCARNATION)[0]
        assert record.result is not None
        assert record.result.claim_generation == 0
    finally:
        ledger.close()


def test_result_write_failure_preserves_provider_error_and_started_record(
    tmp_path,
) -> None:
    class _ResultWriteFailureLedger(AdaptiveBudgetLedger):
        def record_model_result(self, result, *, owner_token):
            raise OSError("model result storage failed")

    ledger = _ResultWriteFailureLedger(tmp_path / "result-write-failure.sqlite")

    def fail(_reservation):
        raise RuntimeError("provider connection was lost")

    try:
        with pytest.raises(RuntimeError, match="provider connection was lost"):
            execute_model_call_with_budget(
                RUN_ID,
                INCARNATION,
                "result-write-failure",
                _request_digest("result-write-failure"),
                MODEL_IDENTITY,
                200,
                100,
                fail,
                config=_config(),
                ledger=ledger,
                claim_now_ns=lambda: 1,
                owner_token_factory=lambda: "result-write-owner",
            )
        record = ledger.load_model_records(RUN_ID, INCARNATION)[0]
        assert record.started is not None
        assert record.result is None
        assert record.reconciliation is None
    finally:
        ledger.close()


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("provider connection was lost"),
        ValueError("malformed provider usage"),
    ],
)
def test_sync_model_failure_after_started_charges_maximum_and_reraises(
    tmp_path, failure: BaseException
) -> None:
    ledger = AdaptiveBudgetLedger(tmp_path / "sync-model-failure.sqlite")
    calls = 0

    def fail(_reservation):
        nonlocal calls
        calls += 1
        raise failure

    try:
        with pytest.raises(type(failure), match=str(failure)):
            execute_model_call_with_budget(
                RUN_ID,
                INCARNATION,
                "sync-model-failure",
                _request_digest("sync-model-failure"),
                MODEL_IDENTITY,
                200,
                100,
                fail,
                config=_config(),
                ledger=ledger,
                claim_now_ns=lambda: 1,
                owner_token_factory=lambda: "sync-failure-owner",
            )
        record = ledger.load_model_records(RUN_ID, INCARNATION)[0]
        assert calls == 1
        assert record.started is not None
        assert record.result is not None
        assert record.reconciliation is not None
        assert record.result.usage == ModelTokenUsage(
            input_tokens=None, output_tokens=None
        )
        assert record.reconciliation.charged_total_tokens == 300
    finally:
        ledger.close()


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("provider connection was lost"),
        ValueError("malformed provider usage"),
        asyncio.CancelledError(),
    ],
)
def test_async_model_failure_after_started_charges_maximum_and_reraises(
    tmp_path, failure: BaseException
) -> None:
    ledger = AdaptiveBudgetLedger(tmp_path / "async-model-failure.sqlite")
    calls = 0

    async def fail(_reservation):
        nonlocal calls
        calls += 1
        raise failure

    async def run() -> None:
        with pytest.raises(type(failure), match=str(failure)) as raised:
            await execute_model_call_with_budget_async(
                RUN_ID,
                INCARNATION,
                "async-model-failure",
                _request_digest("async-model-failure"),
                MODEL_IDENTITY,
                200,
                100,
                fail,
                config=_config(),
                ledger=ledger,
                claim_now_ns=lambda: 1,
                owner_token_factory=lambda: "async-failure-owner",
            )
        assert raised.value is failure

    try:
        asyncio.run(run())
        record = ledger.load_model_records(RUN_ID, INCARNATION)[0]
        assert calls == 1
        assert record.result is not None
        assert record.reconciliation is not None
        assert record.result.usage == ModelTokenUsage(
            input_tokens=None, output_tokens=None
        )
        assert record.reconciliation.charged_total_tokens == 300
    finally:
        ledger.close()


@pytest.mark.parametrize(
    "wait_error",
    [RuntimeError("follower wait failed"), asyncio.CancelledError()],
)
def test_async_follower_wait_error_never_calls_provider(
    tmp_path, wait_error: BaseException
) -> None:
    ledger = AdaptiveBudgetLedger(tmp_path / "follower-wait-error.sqlite")
    reservation = reserve_model_call_budget(
        RUN_ID,
        INCARNATION,
        "follower-wait-error",
        _request_digest("follower-wait-error"),
        MODEL_IDENTITY,
        200,
        100,
        config=_config(),
        ledger=ledger,
    )
    claim = ledger.claim_model_execution(reservation, "leader", now_ns=1)
    assert claim.acquired is True
    started_values = {
        "reservation": reservation,
        "invocation_id": "leader-invocation",
        "claim_generation": claim.generation,
        "started_at_ns": 2,
    }
    ledger.record_model_started(
        ModelCallStarted(
            **started_values,
            started_digest=canonical_digest(started_values),
        ),
        owner_token="leader",
    )
    provider_calls = 0

    async def provider(_reservation):
        nonlocal provider_calls
        provider_calls += 1
        return ModelTokenUsage(input_tokens=1, output_tokens=1)

    async def fail_wait(_seconds: float) -> None:
        raise wait_error

    async def run() -> None:
        with pytest.raises(type(wait_error), match=str(wait_error)):
            await execute_model_call_with_budget_async(
                RUN_ID,
                INCARNATION,
                "follower-wait-error",
                _request_digest("follower-wait-error"),
                MODEL_IDENTITY,
                200,
                100,
                provider,
                config=_config(),
                ledger=ledger,
                claim_now_ns=lambda: 2,
                owner_token_factory=lambda: "follower",
                wait=fail_wait,
            )

    try:
        asyncio.run(run())
        assert provider_calls == 0
        record = ledger.load_model_records(RUN_ID, INCARNATION)[0]
        assert record.started is not None
        assert record.result is None
        assert record.reconciliation is None
    finally:
        ledger.close()


def test_conflicting_model_result_replay_is_rejected(tmp_path) -> None:
    ledger = AdaptiveBudgetLedger(tmp_path / "result-conflict.sqlite")
    try:
        execute_model_call_with_budget(
            RUN_ID,
            INCARNATION,
            "result-call",
            _request_digest("result"),
            MODEL_IDENTITY,
            200,
            100,
            lambda _reservation: ModelTokenUsage(input_tokens=10, output_tokens=5),
            config=_config(),
            ledger=ledger,
            claim_now_ns=lambda: 1,
            owner_token_factory=lambda: "owner",
        )
        record = ledger.load_model_records(RUN_ID, INCARNATION)[0]
        assert record.started is not None
        assert record.result is not None
        reservation_values = {
            "run_id": record.reservation.run_id,
            "run_incarnation": record.reservation.run_incarnation,
            "call_id": record.reservation.call_id,
            "request_digest": _request_digest("conflicting-result-reservation"),
            "model_identity": record.reservation.model_identity,
            "policy_digest": record.reservation.policy_digest,
            "budget_before": record.reservation.budget_before,
            "maximum_input_tokens": record.reservation.maximum_input_tokens,
            "maximum_output_tokens": record.reservation.maximum_output_tokens,
        }
        conflicting_reservation = ModelCallReservation(
            **reservation_values,
            reservation_digest=canonical_digest(reservation_values),
        )

        for conflicting in (
            _model_result(
                record.reservation,
                record.started.invocation_id,
                ModelTokenUsage(input_tokens=11, output_tokens=5),
            ),
            _model_result(
                record.reservation,
                "different-invocation",
                record.result.usage,
            ),
            _model_result(
                conflicting_reservation,
                record.started.invocation_id,
                record.result.usage,
            ),
        ):
            with pytest.raises(BudgetConflictError, match="conflicting"):
                ledger.record_model_result(conflicting, owner_token="owner")
        assert ledger.load_model_records(RUN_ID, INCARNATION)[0].result == record.result
    finally:
        ledger.close()


def test_lease_takeover_before_started_executes_provider_once(tmp_path) -> None:
    path = tmp_path / "before-started.sqlite"
    first = AdaptiveBudgetLedger(path)
    reservation = reserve_model_call_budget(
        RUN_ID,
        INCARNATION,
        "before-started",
        _request_digest("before-started"),
        MODEL_IDENTITY,
        200,
        100,
        config=_config(),
        ledger=first,
    )
    claim = first.claim_model_execution(reservation, "lost-owner", now_ns=1)
    assert claim.acquired is True
    assert claim.generation == 0
    first.close()

    restarted = AdaptiveBudgetLedger(path)
    calls = 0

    def execute(_reservation):
        nonlocal calls
        calls += 1
        return ModelTokenUsage(input_tokens=10, output_tokens=5)

    try:
        reconciliation = execute_model_call_with_budget(
            RUN_ID,
            INCARNATION,
            "before-started",
            _request_digest("before-started"),
            MODEL_IDENTITY,
            200,
            100,
            execute,
            config=_config(),
            ledger=restarted,
            claim_now_ns=lambda: 1 + EXECUTION_CLAIM_LEASE_NS,
            owner_token_factory=lambda: "takeover-owner",
        )
        assert calls == 1
        assert reconciliation.charged_total_tokens == 15
        record = restarted.load_model_records(RUN_ID, INCARNATION)[0]
        assert record.started is not None
        assert record.started.claim_generation == 1
        assert record.result is not None
        assert record.result.claim_generation == 1
    finally:
        restarted.close()


def test_result_without_reconciliation_is_recovered_without_provider_replay(
    tmp_path,
) -> None:
    path = tmp_path / "after-result.sqlite"
    first = AdaptiveBudgetLedger(path)
    reservation = reserve_model_call_budget(
        RUN_ID,
        INCARNATION,
        "after-result",
        _request_digest("after-result"),
        MODEL_IDENTITY,
        200,
        100,
        config=_config(),
        ledger=first,
    )
    claim = first.claim_model_execution(reservation, "first-owner", now_ns=1)
    assert claim.acquired is True
    assert claim.generation == 0
    started_values = {
        "reservation": reservation,
        "invocation_id": "after-result-invocation",
        "claim_generation": claim.generation,
        "started_at_ns": 2,
    }
    started, created = first.record_model_started(
        ModelCallStarted(
            **started_values,
            started_digest=canonical_digest(started_values),
        ),
        owner_token="first-owner",
    )
    assert created is True
    first.record_model_result(
        _model_result(
            reservation,
            started.invocation_id,
            ModelTokenUsage(input_tokens=10, output_tokens=5),
        ),
        owner_token="first-owner",
    )
    first.close()

    restarted = AdaptiveBudgetLedger(path)
    invoked = False

    def must_not_execute(_reservation):
        nonlocal invoked
        invoked = True
        return ModelTokenUsage(input_tokens=1, output_tokens=1)

    try:
        reconciliation = execute_model_call_with_budget(
            RUN_ID,
            INCARNATION,
            "after-result",
            _request_digest("after-result"),
            MODEL_IDENTITY,
            200,
            100,
            must_not_execute,
            config=_config(),
            ledger=restarted,
            claim_now_ns=lambda: 3,
            owner_token_factory=lambda: "recovery-owner",
        )
        assert invoked is False
        assert reconciliation.charged_total_tokens == 15
    finally:
        restarted.close()


def test_two_concurrent_owners_execute_and_charge_once(tmp_path) -> None:
    ledger = AdaptiveBudgetLedger(tmp_path / "concurrent.sqlite")
    provider_started = threading.Event()
    release_provider = threading.Event()
    calls = 0
    lock = threading.Lock()

    def execute(_reservation):
        nonlocal calls
        with lock:
            calls += 1
        provider_started.set()
        assert release_provider.wait(timeout=5)
        return ModelTokenUsage(input_tokens=10, output_tokens=5)

    def run(owner):
        return execute_model_call_with_budget(
            RUN_ID,
            INCARNATION,
            "concurrent-call",
            _request_digest("concurrent"),
            MODEL_IDENTITY,
            200,
            100,
            execute,
            config=_config(),
            ledger=ledger,
            claim_now_ns=lambda: 1,
            owner_token_factory=lambda: owner,
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(run, owner) for owner in ("owner-a", "owner-b")]
            assert provider_started.wait(timeout=5)
            release_provider.set()
            reconciliations = [future.result(timeout=5) for future in futures]
        assert calls == 1
        assert reconciliations[0] == reconciliations[1]
        assert reconciliations[0].charged_total_tokens == 15
        assert len(ledger.load_model_records(RUN_ID, INCARNATION)) == 1
    finally:
        ledger.close()


def test_same_owner_race_before_started_executes_provider_once(tmp_path) -> None:
    ledger = _SynchronizedClaimLedger(
        tmp_path / "same-owner.sqlite", threading.Barrier(2)
    )
    calls = 0
    calls_lock = threading.Lock()

    def execute(_reservation):
        nonlocal calls
        with calls_lock:
            calls += 1
        return ModelTokenUsage(input_tokens=10, output_tokens=5)

    def run():
        return execute_model_call_with_budget(
            RUN_ID,
            INCARNATION,
            "same-owner-call",
            _request_digest("same-owner"),
            MODEL_IDENTITY,
            200,
            100,
            execute,
            config=_config(),
            ledger=ledger,
            claim_now_ns=lambda: 1,
            owner_token_factory=lambda: "shared-owner",
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            reconciliations = [
                future.result(timeout=5)
                for future in (executor.submit(run), executor.submit(run))
            ]
        assert calls == 1
        assert reconciliations[0] == reconciliations[1]
        record = ledger.load_model_records(RUN_ID, INCARNATION)[0]
        assert record.started is not None
        assert record.started.claim_generation == 0
        assert record.result is not None
        assert record.result.claim_generation == 0
    finally:
        ledger.close()


@pytest.mark.parametrize("follower_owner", ["shared-owner", "different-owner"])
def test_active_follower_waits_while_provider_is_running(
    tmp_path,
    follower_owner,
) -> None:
    ledger = AdaptiveBudgetLedger(tmp_path / "same-owner-blocked.sqlite")
    provider_started = threading.Event()
    release_provider = threading.Event()
    follower_waiting = threading.Event()
    calls = 0

    def execute(_reservation):
        nonlocal calls
        calls += 1
        provider_started.set()
        assert release_provider.wait(timeout=5)
        return ModelTokenUsage(input_tokens=10, output_tokens=5)

    def run(owner, wait):
        return execute_model_call_with_budget(
            RUN_ID,
            INCARNATION,
            "same-owner-blocked",
            _request_digest("same-owner-blocked"),
            MODEL_IDENTITY,
            200,
            100,
            execute,
            config=_config(),
            ledger=ledger,
            claim_now_ns=lambda: 1,
            owner_token_factory=lambda: owner,
            wait=wait,
        )

    def follower_wait(_seconds):
        follower_waiting.set()
        release_provider.wait(timeout=5)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            leader = executor.submit(run, "shared-owner", lambda _seconds: None)
            assert provider_started.wait(timeout=5)
            follower = executor.submit(run, follower_owner, follower_wait)
            assert follower_waiting.wait(timeout=5)
            record = ledger.load_model_records(RUN_ID, INCARNATION)[0]
            assert calls == 1
            assert record.result is None
            release_provider.set()
            leader_result = leader.result(timeout=5)
            follower_result = follower.result(timeout=5)
        assert leader_result == follower_result
        assert follower_result.usage_was_conservative is False
    finally:
        release_provider.set()
        ledger.close()


@pytest.mark.parametrize("takeover_owner", ["new-owner", "shared-owner"])
def test_expired_started_call_is_settled_once_and_stale_result_is_fenced(
    tmp_path,
    takeover_owner,
) -> None:
    ledger = AdaptiveBudgetLedger(tmp_path / f"takeover-{takeover_owner}.sqlite")
    provider_started = threading.Event()
    release_provider = threading.Event()
    calls = 0

    def execute(_reservation):
        nonlocal calls
        calls += 1
        provider_started.set()
        assert release_provider.wait(timeout=5)
        return ModelTokenUsage(input_tokens=10, output_tokens=5)

    def run_first():
        return execute_model_call_with_budget(
            RUN_ID,
            INCARNATION,
            "expired-started",
            _request_digest("expired-started"),
            MODEL_IDENTITY,
            200,
            100,
            execute,
            config=_config(),
            ledger=ledger,
            claim_now_ns=lambda: 1,
            owner_token_factory=lambda: "shared-owner",
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            stale = executor.submit(run_first)
            assert provider_started.wait(timeout=5)
            takeover = execute_model_call_with_budget(
                RUN_ID,
                INCARNATION,
                "expired-started",
                _request_digest("expired-started"),
                MODEL_IDENTITY,
                200,
                100,
                execute,
                config=_config(),
                ledger=ledger,
                claim_now_ns=lambda: 1 + EXECUTION_CLAIM_LEASE_NS,
                owner_token_factory=lambda: takeover_owner,
            )
            assert takeover.usage_was_conservative is True
            assert takeover.charged_total_tokens == 300
            assert calls == 1
            release_provider.set()
            with pytest.raises(BudgetConflictError, match="claim|generation"):
                stale.result(timeout=5)
        record = ledger.load_model_records(RUN_ID, INCARNATION)[0]
        assert record.started is not None
        assert record.started.claim_generation == 0
        assert record.result is not None
        assert record.result.started_claim_generation == 0
        assert record.result.claim_generation == 1
        assert record.reconciliation == takeover
    finally:
        release_provider.set()
        ledger.close()


@pytest.mark.parametrize(
    "usage",
    [
        ModelTokenUsage(input_tokens=0, output_tokens=0),
        ModelTokenUsage(input_tokens=None, output_tokens=0),
        ModelTokenUsage(input_tokens=0, output_tokens=None),
        ModelTokenUsage(input_tokens=10, output_tokens=5),
    ],
)
def test_takeover_result_dto_rejects_any_reported_usage(tmp_path, usage) -> None:
    ledger = AdaptiveBudgetLedger(tmp_path / "takeover-result-dto.sqlite")
    try:
        reservation = reserve_model_call_budget(
            RUN_ID,
            INCARNATION,
            "takeover-result-dto",
            _request_digest("takeover-result-dto"),
            MODEL_IDENTITY,
            200,
            100,
            config=_config(),
            ledger=ledger,
        )
        with pytest.raises(ValidationError, match="conservative|usage"):
            _model_result(
                reservation,
                "takeover-invocation",
                usage,
                claim_generation=1,
                started_claim_generation=0,
            )
    finally:
        ledger.close()


def test_ledger_rejects_forged_takeover_usage_and_accepts_conservative(
    tmp_path,
) -> None:
    ledger = AdaptiveBudgetLedger(tmp_path / "takeover-result-ledger.sqlite")
    try:
        reservation = reserve_model_call_budget(
            RUN_ID,
            INCARNATION,
            "takeover-result-ledger",
            _request_digest("takeover-result-ledger"),
            MODEL_IDENTITY,
            200,
            100,
            config=_config(),
            ledger=ledger,
        )
        first_claim = ledger.claim_model_execution(reservation, "first-owner", now_ns=1)
        started_values = {
            "reservation": reservation,
            "invocation_id": "takeover-invocation",
            "claim_generation": first_claim.generation,
            "started_at_ns": 2,
        }
        started, created = ledger.record_model_started(
            ModelCallStarted(
                **started_values,
                started_digest=canonical_digest(started_values),
            ),
            owner_token="first-owner",
        )
        assert created is True
        takeover = ledger.claim_model_execution(
            reservation,
            "takeover-owner",
            now_ns=1 + EXECUTION_CLAIM_LEASE_NS,
        )
        assert takeover.acquired is True
        assert takeover.generation == 1

        forged = _model_result(
            reservation,
            started.invocation_id,
            ModelTokenUsage(input_tokens=0, output_tokens=0),
            claim_generation=takeover.generation,
            started_claim_generation=takeover.generation,
        )
        with pytest.raises(BudgetConflictError, match="started.*generation"):
            ledger.record_model_result(forged, owner_token="takeover-owner")

        conservative = _model_result(
            reservation,
            started.invocation_id,
            ModelTokenUsage(input_tokens=None, output_tokens=None),
            claim_generation=takeover.generation,
            started_claim_generation=started.claim_generation,
        )
        stored = ledger.record_model_result(
            conservative,
            owner_token="takeover-owner",
        )
        reconciliation = reconcile_model_call_usage(stored)
        assert reconciliation.usage_was_conservative is True
        assert reconciliation.charged_input_tokens == 200
        assert reconciliation.charged_output_tokens == 100
    finally:
        ledger.close()


def test_tampered_result_started_generation_is_rejected_on_load_and_open(
    tmp_path,
) -> None:
    path = tmp_path / "tampered-result-generation.sqlite"
    ledger = AdaptiveBudgetLedger(path)
    execute_model_call_with_budget(
        RUN_ID,
        INCARNATION,
        "tampered-result-generation",
        _request_digest("tampered-result-generation"),
        MODEL_IDENTITY,
        200,
        100,
        lambda _reservation: ModelTokenUsage(input_tokens=10, output_tokens=5),
        config=_config(),
        ledger=ledger,
        claim_now_ns=lambda: 1,
        owner_token_factory=lambda: "owner",
    )
    record = ledger.load_model_records(RUN_ID, INCARNATION)[0]
    assert record.result is not None
    tampered = _model_result(
        record.reservation,
        record.result.invocation_id,
        record.result.usage,
        claim_generation=1,
        started_claim_generation=1,
    )
    with sqlite3.connect(path) as connection:
        trigger_sql = connection.execute(
            """
            SELECT sql FROM sqlite_master
            WHERE type = 'trigger' AND name = 'adaptive_model_budget_events_no_update'
            """
        ).fetchone()[0]
        connection.execute("DROP TRIGGER adaptive_model_budget_events_no_update")
        connection.execute(
            """
            UPDATE adaptive_model_budget_events
            SET payload = ?, identity_digest = ?
            WHERE call_id = ? AND phase = 'result'
            """,
            (
                canonical_json_bytes(tampered),
                tampered.result_digest,
                record.reservation.call_id,
            ),
        )
        connection.execute(trigger_sql)
        connection.execute(
            """
            UPDATE adaptive_model_budget_execution_claims
            SET generation = 1
            WHERE call_id = ?
            """,
            (record.reservation.call_id,),
        )

    with pytest.raises(BudgetAdmissionError, match="record|generation"):
        ledger.load_model_records(RUN_ID, INCARNATION)
    ledger.close()
    with pytest.raises(BudgetAdmissionError, match="record|generation"):
        AdaptiveBudgetLedger(path)


def test_claim_generation_reaches_sqlite_max_then_fails_closed(tmp_path) -> None:
    path = tmp_path / "claim-generation-overflow.sqlite"
    ledger = AdaptiveBudgetLedger(path)
    try:
        reservation = reserve_model_call_budget(
            RUN_ID,
            INCARNATION,
            "claim-generation-overflow",
            _request_digest("claim-generation-overflow"),
            MODEL_IDENTITY,
            200,
            100,
            config=_config(),
            ledger=ledger,
        )
        assert ledger.claim_model_execution(
            reservation,
            "initial-owner",
            now_ns=1,
        ).acquired
        with sqlite3.connect(path) as connection:
            connection.execute(
                """
                UPDATE adaptive_model_budget_execution_claims
                SET generation = ?
                WHERE call_id = ?
                """,
                (SQLITE_SIGNED_INTEGER_MAX - 1, reservation.call_id),
            )
        first_expiry = 1 + EXECUTION_CLAIM_LEASE_NS
        maximum_claim = ledger.claim_model_execution(
            reservation,
            "maximum-owner",
            now_ns=first_expiry,
        )
        assert maximum_claim.acquired is True
        assert maximum_claim.generation == SQLITE_SIGNED_INTEGER_MAX
        with sqlite3.connect(path) as connection:
            before = connection.execute(
                """
                SELECT owner_token, claimed_at_ns, lease_expires_ns, generation
                FROM adaptive_model_budget_execution_claims
                WHERE call_id = ?
                """,
                (reservation.call_id,),
            ).fetchone()

        with pytest.raises(BudgetConflictError, match="generation"):
            ledger.claim_model_execution(
                reservation,
                "overflow-owner",
                now_ns=first_expiry + EXECUTION_CLAIM_LEASE_NS,
            )
        with sqlite3.connect(path) as connection:
            after = connection.execute(
                """
                SELECT owner_token, claimed_at_ns, lease_expires_ns, generation
                FROM adaptive_model_budget_execution_claims
                WHERE call_id = ?
                """,
                (reservation.call_id,),
            ).fetchone()
        assert after == before
    finally:
        ledger.close()


def test_model_generation_dto_rejects_value_above_sqlite_max(tmp_path) -> None:
    ledger = AdaptiveBudgetLedger(tmp_path / "generation-dto-overflow.sqlite")
    try:
        reservation = reserve_model_call_budget(
            RUN_ID,
            INCARNATION,
            "generation-dto-overflow",
            _request_digest("generation-dto-overflow"),
            MODEL_IDENTITY,
            200,
            100,
            config=_config(),
            ledger=ledger,
        )
        started_values = {
            "reservation": reservation,
            "invocation_id": "overflow-invocation",
            "claim_generation": SQLITE_SIGNED_INTEGER_MAX + 1,
            "started_at_ns": 1,
        }
        with pytest.raises(ValidationError, match="claim_generation"):
            ModelCallStarted(
                **started_values,
                started_digest=canonical_digest(started_values),
            )
        with pytest.raises(ValidationError, match="claim_generation"):
            _model_result(
                reservation,
                "overflow-invocation",
                ModelTokenUsage(input_tokens=1, output_tokens=1),
                claim_generation=SQLITE_SIGNED_INTEGER_MAX + 1,
            )
    finally:
        ledger.close()


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE adaptive_model_budget_execution_claims "
        f"SET generation = {SQLITE_SIGNED_INTEGER_MAX + 1}",
        "UPDATE adaptive_model_budget_execution_claims "
        "SET generation = 'not-an-integer'",
    ],
)
def test_model_claim_reopen_rejects_out_of_range_or_typed_generation(
    tmp_path,
    mutation,
) -> None:
    path = tmp_path / "invalid-model-claim-generation.sqlite"
    ledger = AdaptiveBudgetLedger(path)
    reservation = reserve_model_call_budget(
        RUN_ID,
        INCARNATION,
        "invalid-model-claim-generation",
        _request_digest("invalid-model-claim-generation"),
        MODEL_IDENTITY,
        200,
        100,
        config=_config(),
        ledger=ledger,
    )
    assert ledger.claim_model_execution(reservation, "owner", now_ns=1).acquired
    ledger.close()
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(mutation)

    with pytest.raises(BudgetAdmissionError, match="claim"):
        AdaptiveBudgetLedger(path)


def test_generation_tampering_and_conflicting_events_are_rejected(tmp_path) -> None:
    ledger = AdaptiveBudgetLedger(tmp_path / "generation-tamper.sqlite")
    try:
        reservation = reserve_model_call_budget(
            RUN_ID,
            INCARNATION,
            "generation-tamper",
            _request_digest("generation-tamper"),
            MODEL_IDENTITY,
            200,
            100,
            config=_config(),
            ledger=ledger,
        )
        claim = ledger.claim_model_execution(reservation, "owner", now_ns=1)
        assert claim.acquired is True
        assert claim.generation == 0

        forged_values = {
            "reservation": reservation,
            "invocation_id": "forged-generation",
            "claim_generation": 1,
            "started_at_ns": 2,
        }
        forged = ModelCallStarted(
            **forged_values,
            started_digest=canonical_digest(forged_values),
        )
        with pytest.raises(BudgetConflictError, match="generation"):
            ledger.record_model_started(forged, owner_token="owner")

        started_values = {
            "reservation": reservation,
            "invocation_id": "generation-zero",
            "claim_generation": 0,
            "started_at_ns": 2,
        }
        started = ModelCallStarted(
            **started_values,
            started_digest=canonical_digest(started_values),
        )
        stored, created = ledger.record_model_started(started, owner_token="owner")
        assert stored == started
        assert created is True
        assert ledger.record_model_started(started, owner_token="owner") == (
            started,
            False,
        )

        conflicting_values = {
            **started_values,
            "invocation_id": "conflicting-invocation",
        }
        conflicting_started = ModelCallStarted(
            **conflicting_values,
            started_digest=canonical_digest(conflicting_values),
        )
        with pytest.raises(BudgetConflictError, match="conflicting"):
            ledger.record_model_started(conflicting_started, owner_token="owner")

        forged_result = _model_result(
            reservation,
            started.invocation_id,
            ModelTokenUsage(input_tokens=10, output_tokens=5),
            claim_generation=1,
        )
        with pytest.raises(BudgetConflictError, match="generation"):
            ledger.record_model_result(forged_result, owner_token="owner")
        result = ledger.record_model_result(
            _model_result(
                reservation,
                started.invocation_id,
                ModelTokenUsage(input_tokens=10, output_tokens=5),
            ),
            owner_token="owner",
        )
        with pytest.raises(BudgetConflictError, match="conflicting"):
            ledger.record_model_result(
                _model_result(
                    reservation,
                    started.invocation_id,
                    ModelTokenUsage(input_tokens=11, output_tokens=5),
                ),
                owner_token="owner",
            )
        assert ledger.load_model_records(RUN_ID, INCARNATION)[0].result == result
    finally:
        ledger.close()


def test_same_owner_claim_is_fenced_across_processes(tmp_path) -> None:
    path = tmp_path / "process-claim.sqlite"
    ledger = AdaptiveBudgetLedger(path)
    reservation = reserve_model_call_budget(
        RUN_ID,
        INCARNATION,
        "process-claim",
        _request_digest("process-claim"),
        MODEL_IDENTITY,
        200,
        100,
        config=_config(),
        ledger=ledger,
    )
    ledger.close()
    context = multiprocessing.get_context("fork")
    barrier = context.Barrier(2)
    results = context.Queue()
    processes = [
        context.Process(
            target=_claim_model_in_process,
            args=(str(path), reservation.model_dump_json(), barrier, results),
        )
        for _ in range(2)
    ]
    try:
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=10)
        assert all(process.exitcode == 0 for process in processes)
        claims = [results.get(timeout=2) for _ in processes]
        assert sorted(claims) == [(False, 0), (True, 0)]
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=2)
        results.close()
        results.join_thread()


def test_same_owner_synchronized_race_repeats_without_double_execution(
    tmp_path,
) -> None:
    ledger = _SynchronizedClaimLedger(
        tmp_path / "repeated-race.sqlite", threading.Barrier(2)
    )
    calls = 0
    lock = threading.Lock()

    def execute(_reservation):
        nonlocal calls
        with lock:
            calls += 1
        return ModelTokenUsage(input_tokens=1, output_tokens=1)

    def run(iteration):
        return execute_model_call_with_budget(
            f"race-run-{iteration}",
            INCARNATION,
            "call",
            _request_digest(f"race-{iteration}"),
            MODEL_IDENTITY,
            2,
            2,
            execute,
            config=_config(),
            ledger=ledger,
            claim_now_ns=lambda: 1,
            owner_token_factory=lambda: "shared-owner",
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            for iteration in range(100):
                if iteration:
                    ledger.synchronize_next_claims(threading.Barrier(2))
                futures = (
                    executor.submit(run, iteration),
                    executor.submit(run, iteration),
                )
                assert futures[0].result(timeout=5) == futures[1].result(timeout=5)
        assert calls == 100
    finally:
        ledger.close()


def test_v1_ledger_migrates_additively_without_changing_probe_tables(tmp_path) -> None:
    path = tmp_path / "v1-ledger.sqlite"
    with sqlite3.connect(path) as connection:
        for statement in _V1_SCHEMA_SQL:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO adaptive_budget_meta (key, value) VALUES ('schema_version', 1)"
        )

    ledger = AdaptiveBudgetLedger(path)
    try:
        with sqlite3.connect(path) as connection:
            version = connection.execute(
                "SELECT value FROM adaptive_budget_meta WHERE key = 'schema_version'"
            ).fetchone()[0]
            names = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        assert version == ADAPTIVE_BUDGET_SCHEMA_VERSION
        assert "adaptive_budget_events" in names
        assert "adaptive_model_budget_events" in names
    finally:
        ledger.close()


def test_v1_to_v2_migration_preserves_existing_probe_event_bytes(tmp_path) -> None:
    path = tmp_path / "v1-with-data.sqlite"
    config = _config(policy_version=1)
    values = {
        "run_id": "probe-run",
        "run_incarnation": "probe-incarnation",
        "revision": 0,
        "schema_namespace_version": "sha256:" + "a" * 64,
        "action_digest": "sha256:" + "b" * 64,
        "probe_kind": ResearchActionKind.INSPECT_TABLE,
        "target": TableRef(namespace="main", schema=None, table="orders"),
        "policy_digest": canonical_digest(config),
        "budget_before": initial_budget_state(config),
        "maximum_cost": EvidenceCost(
            wall_clock_ms=1,
            model_calls=0,
            model_tokens=0,
            db_probe_ms=1,
            rows=0,
            bytes=0,
        ),
    }
    reservation = BudgetReservation(
        **values,
        reservation_digest=canonical_digest(values),
    )
    ledger = AdaptiveBudgetLedger(path)
    ledger.record_reservation(reservation)
    ledger.close()

    with sqlite3.connect(path) as connection:
        before = connection.execute(
            "SELECT payload, identity_digest FROM adaptive_budget_events"
        ).fetchone()
        connection.execute("DROP TABLE adaptive_model_budget_execution_claims")
        connection.execute("DROP TABLE adaptive_model_budget_events")
        connection.execute(
            "UPDATE adaptive_budget_meta SET value = 1 WHERE key = 'schema_version'"
        )

    AdaptiveBudgetLedger(path).close()
    with sqlite3.connect(path) as connection:
        after = connection.execute(
            "SELECT payload, identity_digest FROM adaptive_budget_events"
        ).fetchone()
        version = connection.execute(
            "SELECT value FROM adaptive_budget_meta WHERE key = 'schema_version'"
        ).fetchone()
    assert after == before
    assert version == (ADAPTIVE_BUDGET_SCHEMA_VERSION,)


@pytest.mark.parametrize(
    ("partial_statements", "cleanup_statement"),
    [
        ((_MODEL_SCHEMA_SQL[0],), "DROP TABLE adaptive_model_budget_events"),
        (
            (_MODEL_SCHEMA_SQL[1],),
            "DROP TABLE adaptive_model_budget_execution_claims",
        ),
        (
            (_MODEL_SCHEMA_SQL[0], _MODEL_SCHEMA_SQL[2]),
            "DROP TABLE adaptive_model_budget_events",
        ),
        (
            (_MODEL_SCHEMA_SQL[0], _MODEL_SCHEMA_SQL[3]),
            "DROP TABLE adaptive_model_budget_events",
        ),
        (
            (
                "CREATE INDEX adaptive_model_budget_partial_index "
                "ON adaptive_budget_events(run_id)",
            ),
            "DROP INDEX adaptive_model_budget_partial_index",
        ),
    ],
)
def test_partial_v2_schema_fails_typed_without_mutating_v1_and_can_retry(
    tmp_path,
    partial_statements,
    cleanup_statement,
) -> None:
    path = tmp_path / "partial-v2.sqlite"
    with sqlite3.connect(path) as connection:
        for statement in _V1_SCHEMA_SQL:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO adaptive_budget_meta (key, value) VALUES ('schema_version', 1)"
        )
        for statement in partial_statements:
            connection.execute(statement)

    with pytest.raises(BudgetAdmissionError, match="partial|incompatible"):
        AdaptiveBudgetLedger(path)

    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT value FROM adaptive_budget_meta WHERE key = 'schema_version'"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM adaptive_budget_events"
        ).fetchone() == (0,)
        connection.execute(cleanup_statement)

    AdaptiveBudgetLedger(path).close()
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT value FROM adaptive_budget_meta WHERE key = 'schema_version'"
        ).fetchone() == (ADAPTIVE_BUDGET_SCHEMA_VERSION,)
