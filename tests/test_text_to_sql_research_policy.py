"""W2-03 tests for durable research budget admission and reconciliation."""

from __future__ import annotations

import asyncio
import ast
from concurrent.futures import ThreadPoolExecutor
from dataclasses import is_dataclass
from datetime import UTC, datetime, timedelta
import importlib
from pathlib import Path
import sqlite3
import threading
import time

import pytest
from pydantic import ValidationError
import yaml

from custom_tools.text_to_sql.adaptive import _policy_probe_budget as _probe_policy
from custom_tools.text_to_sql.adaptive.models import (
    BudgetState,
    EvidenceCost,
    ExpectedResultShape,
    QuerySpec,
    ResearchAction,
    ResearchActionKind,
    ResearchState,
    TableRef,
)
from custom_tools.text_to_sql.adaptive.model_budget import ModelBudgetLimits
from custom_tools.text_to_sql.adaptive.policy import (
    MAX_ACTIONS,
    MAX_DB_PROBE_MS,
    MAX_DB_PROBES,
    MAX_INLINE_BYTES,
    MAX_MODEL_DECISIONS,
    MAX_MODEL_TOKENS,
    MAX_RETURNED_ROWS,
    MAX_SAMPLE_ROWS,
    MAX_WALL_CLOCK_SECONDS,
    ActionIdentityError,
    AdaptivePolicyConfig,
    BudgetAdmissionError,
    BudgetConflictError,
    BudgetExhaustedError,
    BudgetReconciliation,
    BudgetReconciliationError,
    BudgetReservation,
    OperationCountBudget,
    PerActionBudget,
    ProbeExecutionFailure,
    ResearchPolicyConfigError,
    ResourceBudget,
    ResultVolumeBudget,
    WallClockBudget,
    canonical_action_digest,
    execute_probe_with_budget,
    initial_budget_state,
    load_adaptive_policy_config,
    reconcile_probe_cost,
    recover_probe_with_budget,
    reserve_probe_budget,
    reset_adaptive_policy_config_cache,
)
from custom_tools.text_to_sql.adaptive.probes import (
    ProbeResult,
    ProbeStatus,
    build_probe_result,
)
from custom_tools.text_to_sql.adaptive.serialization import (
    canonical_digest,
    canonical_json_bytes,
)
from custom_tools.text_to_sql.adaptive.state import apply_research_transition
from workflow.adaptive_budget_ledger import (
    ADAPTIVE_BUDGET_SCHEMA_VERSION,
    EXECUTION_CLAIM_LEASE_NS,
    AdaptiveBudgetLedger,
)
from workflow.adaptive_state_store import (
    AdaptiveCheckpointKey,
    AdaptiveLoopKind,
    AdaptiveStateStore,
)


RUN_ID = "run-policy"
INCARNATION = "incarnation-policy"
SCHEMA = "sha256:" + "a" * 64
NOW = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
CONFIG_ENV = "TEXT_TO_SQL_ADAPTIVE_CONFIG_PATH"


def test_policy_facade_exports_the_historical_public_contract() -> None:
    policy = importlib.import_module("custom_tools.text_to_sql.adaptive.policy")
    expected = {
        "ActionIdentityError",
        "AdaptivePolicyConfig",
        "BudgetAdmissionError",
        "BudgetConflictError",
        "BudgetExhaustedError",
        "BudgetLedger",
        "BudgetLedgerRecord",
        "BudgetReconciliation",
        "BudgetReconciliationError",
        "BudgetReservation",
        "FOLLOWER_POLL_SECONDS",
        "MAX_ACTIONS",
        "MAX_DB_PROBES",
        "MAX_DB_PROBE_MS",
        "MAX_INLINE_BYTES",
        "MAX_MODEL_CALLS_V2",
        "MAX_MODEL_DECISIONS",
        "MAX_MODEL_INPUT_TOKENS_PER_CALL",
        "MAX_MODEL_OUTPUT_TOKENS_PER_CALL",
        "MAX_MODEL_TOKENS",
        "MAX_MODEL_TOTAL_TOKENS",
        "MAX_RETURNED_ROWS",
        "MAX_SAMPLE_ROWS",
        "MAX_WALL_CLOCK_SECONDS",
        "MODEL_BUDGET_POLICY_VERSION",
        "ModelBudgetLedger",
        "NonNegativeInt",
        "OperationCountBudget",
        "POLICY_VERSION",
        "PerActionBudget",
        "PositiveInt",
        "ProbeExecutionFailure",
        "ResearchGenerationAuthority",
        "ResearchGenerationAuthorityStatus",
        "ResearchPolicyConfigError",
        "ResearchPolicyError",
        "ResourceBudget",
        "ResultVolumeBudget",
        "WallClockBudget",
        "canonical_action_digest",
        "canonical_digest_for_action",
        "completed_model_budget_chain",
        "execute_model_call_with_budget",
        "execute_model_call_with_budget_async",
        "execute_probe_with_budget",
        "evaluate_research_generation_authority",
        "initial_budget_state",
        "initial_model_budget_state",
        "load_adaptive_policy_config",
        "reconcile_model_call_usage",
        "reconcile_probe_cost",
        "recover_probe_with_budget",
        "reserve_model_call_budget",
        "reserve_probe_budget",
        "reset_adaptive_policy_config_cache",
        "validate_state_model_budget_policy",
    }

    assert set(policy.__all__) == expected
    assert all(hasattr(policy, name) for name in expected)


def test_private_policy_modules_have_no_facade_dependency_cycle() -> None:
    policy = importlib.import_module("custom_tools.text_to_sql.adaptive.policy")
    module_names = {
        "_policy_action_identity",
        "_policy_common",
        "_policy_config",
        "_policy_model_budget",
        "_policy_probe_budget",
    }
    dependencies: dict[str, set[str]] = {}
    for module_name in module_names:
        module_path = Path(policy.__file__).parent / f"{module_name}.py"
        parsed = ast.parse(module_path.read_text())
        imported_modules = {
            node.module
            for node in ast.walk(parsed)
            if isinstance(node, ast.ImportFrom)
            and node.level == 1
            and node.module is not None
        }
        assert "policy" not in imported_modules
        dependencies[module_name] = imported_modules & module_names

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(module_name: str) -> None:
        assert module_name not in visiting
        if module_name in visited:
            return
        visiting.add(module_name)
        for dependency in dependencies[module_name]:
            visit(dependency)
        visiting.remove(module_name)
        visited.add(module_name)

    for module_name in module_names:
        visit(module_name)


def test_policy_facade_reexports_original_objects_without_wrappers() -> None:
    policy = importlib.import_module("custom_tools.text_to_sql.adaptive.policy")
    owners = {
        "_policy_action_identity": {
            "canonical_action_digest",
            "canonical_digest_for_action",
        },
        "_policy_common": {
            "ActionIdentityError",
            "BudgetAdmissionError",
            "BudgetConflictError",
            "BudgetExhaustedError",
            "BudgetReconciliationError",
            "FOLLOWER_POLL_SECONDS",
            "MAX_ACTIONS",
            "MAX_DB_PROBES",
            "MAX_DB_PROBE_MS",
            "MAX_INLINE_BYTES",
            "MAX_MODEL_CALLS_V2",
            "MAX_MODEL_DECISIONS",
            "MAX_MODEL_INPUT_TOKENS_PER_CALL",
            "MAX_MODEL_OUTPUT_TOKENS_PER_CALL",
            "MAX_MODEL_TOKENS",
            "MAX_MODEL_TOTAL_TOKENS",
            "MAX_RETURNED_ROWS",
            "MAX_SAMPLE_ROWS",
            "MAX_WALL_CLOCK_SECONDS",
            "MODEL_BUDGET_POLICY_VERSION",
            "NonNegativeInt",
            "POLICY_VERSION",
            "PositiveInt",
            "ProbeExecutionFailure",
            "ResearchPolicyConfigError",
            "ResearchPolicyError",
        },
        "_policy_authority": {
            "ResearchGenerationAuthority",
            "ResearchGenerationAuthorityStatus",
            "evaluate_research_generation_authority",
        },
        "_policy_config": {
            "AdaptivePolicyConfig",
            "OperationCountBudget",
            "PerActionBudget",
            "ResourceBudget",
            "ResultVolumeBudget",
            "WallClockBudget",
            "initial_budget_state",
            "initial_model_budget_state",
            "load_adaptive_policy_config",
            "reset_adaptive_policy_config_cache",
        },
        "_policy_model_budget": {
            "ModelBudgetLedger",
            "completed_model_budget_chain",
            "execute_model_call_with_budget",
            "execute_model_call_with_budget_async",
            "reconcile_model_call_usage",
            "reserve_model_call_budget",
            "validate_state_model_budget_policy",
        },
        "_policy_probe_budget": {
            "BudgetLedger",
            "BudgetLedgerRecord",
            "BudgetReconciliation",
            "BudgetReservation",
            "execute_probe_with_budget",
            "reconcile_probe_cost",
            "recover_probe_with_budget",
            "reserve_probe_budget",
        },
    }

    assert set().union(*owners.values()) == set(policy.__all__)
    for module_name, names in owners.items():
        module = importlib.import_module(
            f"custom_tools.text_to_sql.adaptive.{module_name}"
        )
        for name in names:
            assert getattr(policy, name) is getattr(module, name)

    model_budget = importlib.import_module(
        "custom_tools.text_to_sql.adaptive._policy_model_budget"
    )
    assert is_dataclass(model_budget._ModelExecutionReady)
    assert model_budget._ModelExecutionReady.__slots__ == (
        "started",
        "claim_generation",
    )


@pytest.fixture(autouse=True)
def _reset_config_cache() -> None:
    reset_adaptive_policy_config_cache()
    yield
    reset_adaptive_policy_config_cache()


@pytest.fixture
def budget_ledger(tmp_path: Path):
    path = tmp_path / "adaptive.sqlite"
    ledger = AdaptiveBudgetLedger(path)
    yield ledger, path
    ledger.close()


def _config() -> AdaptivePolicyConfig:
    return AdaptivePolicyConfig(
        policy_version=1,
        wall_clock=WallClockBudget(wall_clock_seconds=MAX_WALL_CLOCK_SECONDS),
        resource_limits=ResourceBudget(
            model_tokens=MAX_MODEL_TOKENS,
            db_probe_ms=MAX_DB_PROBE_MS,
        ),
        operation_counts=OperationCountBudget(
            actions=MAX_ACTIONS,
            model_decisions=MAX_MODEL_DECISIONS,
            db_probes=MAX_DB_PROBES,
        ),
        result_volume=ResultVolumeBudget(
            returned_rows=MAX_RETURNED_ROWS,
            inline_bytes=MAX_INLINE_BYTES,
        ),
        per_action=PerActionBudget(sample_rows=MAX_SAMPLE_ROWS),
    )


def _v2_config() -> AdaptivePolicyConfig:
    limits = ModelBudgetLimits(
        model_calls=2,
        input_tokens_per_call=10,
        output_tokens_per_call=10,
        total_tokens=40,
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


def _table(name: str = "orders") -> TableRef:
    return TableRef(namespace="main", schema=None, table=name)


def _action(
    revision: int,
    *,
    action_id: str | None = None,
    kind: ResearchActionKind = ResearchActionKind.INSPECT_TABLE,
    target: TableRef | None = None,
    parameters: tuple[tuple[str, str | int | float | bool | None], ...] | None = None,
) -> ResearchAction:
    selected_target = target or _table()
    selected_parameters = (
        (("detail", f"full-{revision}"),) if parameters is None else parameters
    )
    return ResearchAction(
        action_id=action_id or f"action-{revision}",
        kind=kind,
        hypothesis_id=None,
        target=selected_target,
        parameters=selected_parameters,
        action_digest=canonical_action_digest(
            kind=kind,
            hypothesis_id=None,
            target=selected_target,
            parameters=selected_parameters,
            expected_revision=revision,
        ),
        expected_revision=revision,
    )


def _cost(
    *,
    wall_clock_ms: int = 1,
    model_calls: int = 0,
    model_tokens: int = 0,
    db_probe_ms: int = 1,
    rows: int = 0,
    bytes_: int = 0,
) -> EvidenceCost:
    return EvidenceCost(
        wall_clock_ms=wall_clock_ms,
        model_calls=model_calls,
        model_tokens=model_tokens,
        db_probe_ms=db_probe_ms,
        rows=rows,
        bytes=bytes_,
    )


def _state(
    *,
    history: tuple[ResearchAction, ...] = (),
    budget: BudgetState | None = None,
) -> ResearchState:
    query = QuerySpec(
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
        revision=0,
        schema_namespace_version=None,
        query_id="query-policy",
        original_text="orders",
        semantic_items=(),
        expected_result_shape=ExpectedResultShape.ROWS,
        global_constraints=(),
    )
    return ResearchState(
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
        revision=len(history),
        schema_namespace_version=SCHEMA,
        query_spec=query,
        hypotheses=(),
        evidence=(),
        bindings=(),
        join_candidates=(),
        unresolved_items=(),
        action_history=history,
        result_expectations=(),
        budget_state=budget or initial_budget_state(_config()),
        stop_reason=None,
    )


def _failed_result(
    action: ResearchAction,
    *,
    invocation_id: str | None = None,
    status: ProbeStatus = ProbeStatus.FAILED,
    cost: EvidenceCost | None = None,
) -> ProbeResult:
    actual = cost or _cost(wall_clock_ms=0, db_probe_ms=0)
    return build_probe_result(
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
        revision=action.expected_revision,
        schema_namespace_version=SCHEMA,
        invocation_id=invocation_id or f"invocation-{action.expected_revision}",
        action_digest=action.action_digest,
        probe_kind=action.kind,
        status=status,
        target=action.target,
        started_at=NOW,
        completed_at=NOW,
        summary="probe did not complete",
        cost=actual,
        row_count=actual.rows,
        failure_code=status.value,
    )


def _complete(
    ledger: AdaptiveBudgetLedger,
    state: ResearchState,
    action: ResearchAction,
    *,
    maximum: EvidenceCost | None = None,
    actual: EvidenceCost | None = None,
) -> ResearchState:
    reservation = reserve_probe_budget(
        state,
        action,
        maximum or _cost(),
        config=_config(),
        ledger=ledger,
    )
    result = _failed_result(action, cost=actual)
    assert ledger.claim_execution(reservation, "complete-owner", now_ns=0)
    result = ledger.record_result(
        reservation,
        result,
        owner_token="complete-owner",
    )
    reconciliation = reconcile_probe_cost(reservation, result)
    ledger.record_reconciliation(reconciliation, result)
    return apply_research_transition(
        state,
        action,
        budget_state=reconciliation.budget_after,
    ).state


def test_recovery_returns_none_without_durable_result_and_never_claims(
    tmp_path: Path,
) -> None:
    class _NoClaimLedger(AdaptiveBudgetLedger):
        def claim_execution(self, *args, **kwargs):
            raise AssertionError("recovery must not claim execution")

    ledger = _NoClaimLedger(tmp_path / "recover-empty.sqlite")
    state = _state()
    action = _action(0)
    maximum = _cost(rows=2)
    reserve_probe_budget(state, action, maximum, config=_config(), ledger=ledger)
    try:
        assert (
            recover_probe_with_budget(
                state,
                action,
                maximum,
                expected_invocation_id="expected-invocation",
                config=_config(),
                ledger=ledger,
            )
            is None
        )
    finally:
        ledger.close()


def test_recovery_reconciles_one_durable_result_without_execution(
    budget_ledger,
) -> None:
    ledger, _ = budget_ledger
    state = _state()
    action = _action(0)
    maximum = _cost(rows=2)
    reservation = reserve_probe_budget(
        state,
        action,
        maximum,
        config=_config(),
        ledger=ledger,
    )
    assert ledger.claim_execution(reservation, "crashed-owner", now_ns=0)
    expected = ledger.record_result(
        reservation,
        _failed_result(action),
        owner_token="crashed-owner",
    )

    recovered = recover_probe_with_budget(
        state,
        action,
        maximum,
        expected_invocation_id="invocation-0",
        config=_config(),
        ledger=ledger,
    )

    assert recovered == expected
    records = ledger.load_records(RUN_ID, INCARNATION)
    assert records[0].reconciliation == reconcile_probe_cost(reservation, expected)


def test_recovery_rejects_durable_result_from_another_invocation_before_reconcile(
    budget_ledger,
) -> None:
    ledger, _ = budget_ledger
    state = _state()
    action = _action(0)
    maximum = _cost(rows=2)
    reservation = reserve_probe_budget(
        state,
        action,
        maximum,
        config=_config(),
        ledger=ledger,
    )
    assert ledger.claim_execution(reservation, "other-owner", now_ns=0)
    ledger.record_result(
        reservation,
        _failed_result(action, invocation_id="other-invocation"),
        owner_token="other-owner",
    )

    with pytest.raises(BudgetReconciliationError, match="invocation"):
        recover_probe_with_budget(
            state,
            action,
            maximum,
            expected_invocation_id="expected-invocation",
            config=_config(),
            ledger=ledger,
        )

    assert ledger.load_records(RUN_ID, INCARNATION)[0].reconciliation is None


def _write_policy(path: Path, value: object) -> None:
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")


def _clock(*values: int):
    iterator = iter(values)
    return lambda: next(iterator)


def _utc_clock(*values: datetime):
    iterator = iter(values)
    return lambda: next(iterator)


def test_policy_config_closes_every_budget_dimension() -> None:
    config = load_adaptive_policy_config()

    assert (
        config.wall_clock.wall_clock_seconds,
        config.resource_limits.db_probe_ms,
    ) == (14_400, 14_400_000)
    assert config.policy_version == 2
    assert config.resource_limits.model_tokens == 524_288
    assert config.operation_counts.actions == 512
    assert config.operation_counts.model_decisions == 256
    assert config.operation_counts.db_probes == 384
    assert config.result_volume.returned_rows == 5_000
    assert config.result_volume.inline_bytes == 2 * 1024 * 1024
    assert config.per_action.sample_rows == 50
    assert config.model_budget is not None
    assert config.model_budget.model_calls == 256
    assert config.model_budget.input_tokens_per_call == 16_384
    assert config.model_budget.output_tokens_per_call == 32_000
    assert config.model_budget.total_tokens == 524_288
    with pytest.raises(ValidationError):
        config.resource_limits.db_probe_ms = 1


@pytest.mark.parametrize(
    "change",
    [
        {"unknown": True},
        {"policy_version": 1},
        {"wall_clock": {"wall_clock_seconds": 14_401}},
        {"resource_limits": {"model_tokens": 1, "db_probe_ms": 14_400_000}},
        {"resource_limits": {"model_tokens": 0, "db_probe_ms": 14_400_001}},
        {"resource_limits": {"model_tokens": 10**12, "db_probe_ms": 10**12}},
    ],
)
def test_policy_config_rejects_unknown_version_and_hidden_widening(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: dict[str, object],
) -> None:
    raw = yaml.safe_load(
        (Path(__file__).parents[1] / "config/text_to_sql/adaptive.yaml").read_text(
            encoding="utf-8"
        )
    )
    raw.update(change)
    path = tmp_path / "adaptive.yaml"
    _write_policy(path, raw)
    monkeypatch.setenv(CONFIG_ENV, str(path))

    with pytest.raises(ResearchPolicyConfigError):
        load_adaptive_policy_config()


def test_policy_config_requires_explicit_resource_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = yaml.safe_load(
        (Path(__file__).parents[1] / "config/text_to_sql/adaptive.yaml").read_text(
            encoding="utf-8"
        )
    )
    raw.pop("resource_limits")
    path = tmp_path / "adaptive.yaml"
    _write_policy(path, raw)
    monkeypatch.setenv(CONFIG_ENV, str(path))

    with pytest.raises(ResearchPolicyConfigError):
        load_adaptive_policy_config()


def test_initial_budget_has_no_caller_tuning_path() -> None:
    budget = initial_budget_state(_config())

    assert budget.initial_model_tokens == 0
    assert budget.initial_db_probe_ms == 14_400_000
    with pytest.raises(TypeError):
        initial_budget_state(_config(), model_tokens=10**12)  # type: ignore[call-arg]


def test_canonical_action_identity_ignores_random_id_and_cas_revision() -> None:
    left = _action(0, action_id="random-a", parameters=(("z", 2), ("a", 1)))
    right = _action(0, action_id="random-b", parameters=(("a", 1), ("z", 2)))

    assert left.action_digest == right.action_digest
    assert _action(1, parameters=left.parameters).action_digest == left.action_digest
    with pytest.raises(ActionIdentityError):
        canonical_action_digest(
            kind=ResearchActionKind.INSPECT_TABLE,
            hypothesis_id=None,
            target=_table(),
            parameters=(("value", float("nan")),),
            expected_revision=0,
        )


def test_reservation_survives_restart_without_caller_history(tmp_path: Path) -> None:
    path = tmp_path / "restart.sqlite"
    first_ledger = AdaptiveBudgetLedger(path)
    state = _state()
    action = _action(0)
    first = reserve_probe_budget(
        state,
        action,
        _cost(rows=2),
        config=_config(),
        ledger=first_ledger,
    )
    first_ledger.close()

    restarted = AdaptiveBudgetLedger(path)
    try:
        assert (
            reserve_probe_budget(
                state,
                action,
                _cost(rows=2),
                config=_config(),
                ledger=restarted,
            )
            == first
        )
        assert restarted.load_records(RUN_ID, INCARNATION)[0].reservation == first
    finally:
        restarted.close()


def test_legacy_probe_result_reopens_byte_exact_without_schema_change(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy-result.sqlite"
    ledger = AdaptiveBudgetLedger(path)
    state = _state()
    action = _action(0)
    payload = {"rows": []}
    actual = _cost(bytes_=len(canonical_json_bytes(payload)))
    reservation = reserve_probe_budget(
        state,
        action,
        _cost(bytes_=100),
        config=_config(),
        ledger=ledger,
    )
    assert ledger.claim_execution(reservation, "legacy-owner", now_ns=0)
    result = build_probe_result(
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
        revision=action.expected_revision,
        schema_namespace_version=SCHEMA,
        invocation_id="legacy-invocation",
        action_digest=action.action_digest,
        probe_kind=action.kind,
        status=ProbeStatus.SUCCESS,
        target=action.target,
        started_at=NOW,
        completed_at=NOW,
        summary="legacy successful result",
        cost=actual,
        row_count=0,
        payload=payload,
    )
    result = ledger.record_result(
        reservation,
        result,
        owner_token="legacy-owner",
    )
    reconciliation = reconcile_probe_cost(reservation, result)
    ledger.record_reconciliation(reconciliation, result)
    before = _budget_result_durable_bytes(path)
    ledger.close()

    reopened = AdaptiveBudgetLedger(path)
    loaded = reopened.load_records(RUN_ID, INCARNATION)
    reopened.close()
    after = _budget_result_durable_bytes(path)

    assert loaded[0].result == result
    assert before == after
    result_bytes, digest, schema_version, _ = before
    assert b'"contract_version":1' in result_bytes
    assert digest == canonical_digest(result)
    assert b'"provenance"' not in result_bytes
    assert schema_version == ADAPTIVE_BUDGET_SCHEMA_VERSION


def _budget_result_durable_bytes(path: Path) -> tuple[bytes, str, int, tuple]:
    with sqlite3.connect(path) as connection:
        payload, digest = connection.execute(
            """
            SELECT payload, identity_digest FROM adaptive_budget_events
            WHERE phase = 'result'
            """
        ).fetchone()
        schema_version = connection.execute(
            """
            SELECT value FROM adaptive_budget_meta WHERE key = 'schema_version'
            """
        ).fetchone()[0]
        objects = tuple(
            connection.execute(
                """
                SELECT type, name, sql FROM sqlite_master
                WHERE lower(name) LIKE 'adaptive_budget_%'
                ORDER BY type, name
                """
            )
        )
    return payload, digest, schema_version, objects


def test_state_cannot_omit_durable_history(budget_ledger) -> None:
    ledger, _ = budget_ledger
    state = _state()
    action = _action(0)
    advanced_without_ledger = apply_research_transition(state, action).state

    with pytest.raises(BudgetAdmissionError, match="omits durable"):
        reserve_probe_budget(
            advanced_without_ledger,
            _action(1),
            _cost(),
            config=_config(),
            ledger=ledger,
        )


def test_outstanding_reservation_blocks_next_action_and_overcommit(
    budget_ledger,
) -> None:
    ledger, _ = budget_ledger
    state = _state()
    first_action = _action(0)
    reserve_probe_budget(
        state,
        first_action,
        _cost(rows=MAX_RETURNED_ROWS),
        config=_config(),
        ledger=ledger,
    )

    with pytest.raises(BudgetConflictError, match="outstanding"):
        reserve_probe_budget(
            state,
            _action(0, target=_table("customers")),
            _cost(rows=MAX_RETURNED_ROWS),
            config=_config(),
            ledger=ledger,
        )
    with pytest.raises(BudgetAdmissionError, match="omits durable"):
        reserve_probe_budget(
            apply_research_transition(state, first_action).state,
            _action(1),
            _cost(rows=1),
            config=_config(),
            ledger=ledger,
        )


def test_concurrent_reservations_admit_exactly_one(tmp_path: Path) -> None:
    ledger = AdaptiveBudgetLedger(tmp_path / "concurrent.sqlite")
    barrier = threading.Barrier(2)

    def reserve(table: str):
        barrier.wait()
        return reserve_probe_budget(
            _state(),
            _action(0, target=_table(table)),
            _cost(rows=MAX_RETURNED_ROWS),
            config=_config(),
            ledger=ledger,
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = tuple(
                future.exception() or future.result()
                for future in (
                    pool.submit(reserve, "orders"),
                    pool.submit(reserve, "customers"),
                )
            )
        assert sum(isinstance(item, BudgetReservation) for item in outcomes) == 1
        assert sum(isinstance(item, BudgetConflictError) for item in outcomes) == 1
        assert len(ledger.load_records(RUN_ID, INCARNATION)) == 1
    finally:
        ledger.close()


def test_twenty_identical_callers_execute_callback_once(tmp_path: Path) -> None:
    ledger = AdaptiveBudgetLedger(tmp_path / "twenty-callers.sqlite")
    state = _state()
    action = _action(0)
    callback_calls = 0
    callback_lock = threading.Lock()

    def execute(_: BudgetReservation) -> ProbeResult:
        nonlocal callback_calls
        with callback_lock:
            callback_calls += 1
        time.sleep(0.05)
        return _failed_result(action, cost=_cost(wall_clock_ms=0, db_probe_ms=0))

    def invoke():
        return execute_probe_with_budget(
            state,
            action,
            _cost(wall_clock_ms=200, db_probe_ms=0),
            execute,
            config=_config(),
            ledger=ledger,
        )

    try:
        with ThreadPoolExecutor(max_workers=20) as pool:
            futures = tuple(pool.submit(invoke) for _ in range(20))
            results = tuple(future.result() for future in futures)
        assert callback_calls == 1
        assert all(item == results[0] for item in results)
        assert (
            ledger.load_records(RUN_ID, INCARNATION)[0].reconciliation
            == (results[0][1])
        )
    finally:
        ledger.close()


def test_same_owner_probe_follower_does_not_execute_callback(tmp_path: Path) -> None:
    ledger = AdaptiveBudgetLedger(tmp_path / "same-owner-probe.sqlite")
    state = _state()
    action = _action(0)
    provider_started = threading.Event()
    release_provider = threading.Event()
    follower_waiting = threading.Event()
    callback_calls = 0

    def execute(_: BudgetReservation) -> ProbeResult:
        nonlocal callback_calls
        callback_calls += 1
        provider_started.set()
        assert release_provider.wait(timeout=5)
        return _failed_result(action, cost=_cost(wall_clock_ms=0, db_probe_ms=0))

    def invoke(wait):
        return execute_probe_with_budget(
            state,
            action,
            _cost(wall_clock_ms=200, db_probe_ms=0),
            execute,
            config=_config(),
            ledger=ledger,
            claim_now_ns=lambda: 1,
            owner_token_factory=lambda: "shared-owner",
            wait=wait,
        )

    def follower_wait(_seconds):
        follower_waiting.set()
        release_provider.wait(timeout=5)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            leader = pool.submit(invoke, lambda _seconds: None)
            assert provider_started.wait(timeout=5)
            follower = pool.submit(invoke, follower_wait)
            assert follower_waiting.wait(timeout=5)
            assert callback_calls == 1
            release_provider.set()
            assert leader.result(timeout=5) == follower.result(timeout=5)
    finally:
        release_provider.set()
        ledger.close()


def test_expired_crash_owner_is_recovered_by_one_follower(budget_ledger) -> None:
    ledger, _ = budget_ledger
    state = _state()
    action = _action(0)
    maximum = _cost(wall_clock_ms=10, db_probe_ms=0)
    reservation = reserve_probe_budget(
        state,
        action,
        maximum,
        config=_config(),
        ledger=ledger,
    )
    assert ledger.claim_execution(reservation, "crashed-owner", now_ns=0)
    calls = 0

    def execute(_: BudgetReservation) -> ProbeResult:
        nonlocal calls
        calls += 1
        return _failed_result(action, cost=_cost(wall_clock_ms=0, db_probe_ms=0))

    result, reconciliation = execute_probe_with_budget(
        state,
        action,
        maximum,
        execute,
        config=_config(),
        ledger=ledger,
        monotonic_ns=_clock(0, 1_000_000),
        utc_now=_utc_clock(NOW),
        claim_now_ns=lambda: EXECUTION_CLAIM_LEASE_NS + 1,
        owner_token_factory=lambda: "recovery-owner",
    )

    assert calls == 1
    assert result.cost.wall_clock_ms == 1
    assert reconciliation.budget_exhausted is False


def test_budget_ledger_coexists_with_controller_same_revision(tmp_path: Path) -> None:
    path = tmp_path / "coexist.sqlite"
    controller = AdaptiveStateStore(path)
    ledger = AdaptiveBudgetLedger(path)
    key = AdaptiveCheckpointKey(
        RUN_ID,
        INCARNATION,
        AdaptiveLoopKind.RESEARCH,
        0,
    )
    controller.record_planned(
        key,
        expected_revision=None,
        action={"controller": "planned"},
    )
    state = _state()
    action = _action(0)
    reservation = reserve_probe_budget(
        state,
        action,
        _cost(),
        config=_config(),
        ledger=ledger,
    )
    assert ledger.claim_execution(reservation, "coexist-owner", now_ns=0)
    result = ledger.record_result(
        reservation,
        _failed_result(action, cost=_cost(wall_clock_ms=0, db_probe_ms=0)),
        owner_token="coexist-owner",
    )
    ledger.record_reconciliation(reconcile_probe_cost(reservation, result), result)

    try:
        snapshot = controller.get_snapshot(key)
        assert snapshot.planned is not None
        assert snapshot.planned.action == {"controller": "planned"}
        assert snapshot.observed is None
        with sqlite3.connect(path) as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM adaptive_budget_events"
            ).fetchone() == (3,)
    finally:
        ledger.close()
        controller.close()


def test_budget_event_rows_are_append_only(tmp_path: Path) -> None:
    path = tmp_path / "append-only-budget.sqlite"
    ledger = AdaptiveBudgetLedger(path)
    reserve_probe_budget(
        _state(),
        _action(0),
        _cost(),
        config=_config(),
        ledger=ledger,
    )
    ledger.close()

    with sqlite3.connect(path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("UPDATE adaptive_budget_events SET phase = 'result'")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("DELETE FROM adaptive_budget_events")


def test_budget_schema_rejects_future_version(tmp_path: Path) -> None:
    path = tmp_path / "future-budget.sqlite"
    AdaptiveBudgetLedger(path).close()
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE adaptive_budget_meta SET value = 3 WHERE key = 'schema_version'"
        )

    with pytest.raises(BudgetAdmissionError, match="version"):
        AdaptiveBudgetLedger(path)


@pytest.mark.parametrize(
    "statement",
    [
        'CREATE TABLE "ADAPTIVE_BUDGET_UNEXPECTED" (value INTEGER)',
        """
        CREATE VIEW "Adaptive_Budget_Unexpected_View"
        AS SELECT value FROM unrelated_budget_shared
        """,
        """
        CREATE INDEX "ADAPTIVE_BUDGET_UNEXPECTED_INDEX"
        ON unrelated_budget_shared(value)
        """,
        """
        CREATE TRIGGER "Adaptive_Budget_Unexpected_Trigger"
        AFTER INSERT ON unrelated_budget_shared
        BEGIN
            SELECT 1;
        END
        """,
    ],
)
def test_budget_owned_namespace_is_case_insensitive(tmp_path: Path, statement) -> None:
    path = tmp_path / "unexpected-budget-object.sqlite"
    AdaptiveBudgetLedger(path).close()
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE unrelated_budget_shared (value INTEGER)")
        connection.execute(statement)

    with pytest.raises(BudgetAdmissionError, match="incompatible"):
        AdaptiveBudgetLedger(path)


def test_budget_schema_allows_unrelated_shared_objects(tmp_path: Path) -> None:
    path = tmp_path / "unrelated-budget-objects.sqlite"
    AdaptiveBudgetLedger(path).close()
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE shared_budget_records (value INTEGER);
            CREATE VIEW shared_budget_records_view
            AS SELECT value FROM shared_budget_records;
            CREATE INDEX shared_budget_records_index
            ON shared_budget_records(value);
            CREATE TRIGGER shared_budget_records_trigger
            AFTER INSERT ON shared_budget_records
            BEGIN
                SELECT 1;
            END;
            """
        )

    AdaptiveBudgetLedger(path).close()


def test_budget_schema_preserves_check_literal_case(tmp_path: Path) -> None:
    path = tmp_path / "budget-check-literal.sqlite"
    AdaptiveBudgetLedger(path).close()
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA writable_schema = ON")
        changed = connection.execute(
            """
            UPDATE sqlite_master
            SET sql = replace(sql, '''reservation''', '''RESERVATION''')
            WHERE type = 'table' AND name = 'adaptive_budget_events'
            """
        ).rowcount
        connection.execute("PRAGMA writable_schema = OFF")
    assert changed == 1

    with pytest.raises(BudgetAdmissionError, match="incompatible"):
        AdaptiveBudgetLedger(path)


def test_budget_claim_reopen_accepts_pending_and_completed_claims(
    tmp_path: Path,
) -> None:
    path = tmp_path / "valid-budget-claims.sqlite"
    ledger = AdaptiveBudgetLedger(path)
    state = _state()
    first_action = _action(0)
    state = _complete(ledger, state, first_action)
    second = reserve_probe_budget(
        state,
        _action(1),
        _cost(),
        config=_config(),
        ledger=ledger,
    )
    assert ledger.claim_execution(second, "pending-owner", now_ns=1)
    ledger.close()

    AdaptiveBudgetLedger(path).close()


def test_budget_claim_reopen_rejects_orphan(tmp_path: Path) -> None:
    path = tmp_path / "orphan-budget-claim.sqlite"
    AdaptiveBudgetLedger(path).close()
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO adaptive_budget_execution_claims (
                run_id, run_incarnation, revision, reservation_digest,
                owner_token, claimed_at_ns, lease_expires_ns, generation
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RUN_ID,
                INCARNATION,
                0,
                "sha256:" + "0" * 64,
                "owner",
                0,
                EXECUTION_CLAIM_LEASE_NS,
                0,
            ),
        )

    with pytest.raises(BudgetAdmissionError, match="missing or conflicting"):
        AdaptiveBudgetLedger(path)


def test_budget_claim_reopen_rejects_duplicate_reservation_digest(
    tmp_path: Path,
) -> None:
    path = tmp_path / "duplicate-budget-claim.sqlite"
    ledger = AdaptiveBudgetLedger(path)
    state = _complete(ledger, _state(), _action(0))
    second = reserve_probe_budget(
        state,
        _action(1),
        _cost(),
        config=_config(),
        ledger=ledger,
    )
    assert ledger.claim_execution(second, "second-owner", now_ns=1)
    ledger.close()
    with sqlite3.connect(path) as connection:
        first_digest = connection.execute(
            """
            SELECT reservation_digest
            FROM adaptive_budget_execution_claims
            WHERE revision = 0
            """
        ).fetchone()[0]
        connection.execute(
            """
            UPDATE adaptive_budget_execution_claims
            SET reservation_digest = ?
            WHERE revision = 1
            """,
            (first_digest,),
        )

    with pytest.raises(BudgetAdmissionError, match="duplicated"):
        AdaptiveBudgetLedger(path)


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE adaptive_budget_execution_claims SET owner_token = ''",
        """
        UPDATE adaptive_budget_execution_claims
        SET lease_expires_ns = claimed_at_ns - 1
        """,
        """
        UPDATE adaptive_budget_execution_claims
        SET owner_token = CAST(x'6f776e6572' AS BLOB)
        """,
        """
        UPDATE adaptive_budget_execution_claims
        SET reservation_digest = 'sha256:0000000000000000000000000000000000000000000000000000000000000000'
        """,
    ],
)
def test_budget_claim_reopen_rejects_invalid_rows(
    tmp_path: Path,
    mutation: str,
) -> None:
    path = tmp_path / "invalid-budget-claim.sqlite"
    ledger = AdaptiveBudgetLedger(path)
    reservation = reserve_probe_budget(
        _state(),
        _action(0),
        _cost(),
        config=_config(),
        ledger=ledger,
    )
    assert ledger.claim_execution(reservation, "owner", now_ns=1)
    ledger.close()
    with sqlite3.connect(path) as connection:
        connection.execute(mutation)

    with pytest.raises(BudgetAdmissionError, match="claim"):
        AdaptiveBudgetLedger(path)


def test_probe_count_is_derived_from_complete_ledger(budget_ledger) -> None:
    ledger, _ = budget_ledger
    state = _state()
    for revision in range(MAX_DB_PROBES):
        action = _action(revision)
        state = _complete(
            ledger,
            state,
            action,
            actual=_cost(wall_clock_ms=0, db_probe_ms=0),
        )

    with pytest.raises(BudgetExhaustedError, match="DB probe count"):
        reserve_probe_budget(
            state,
            _action(MAX_DB_PROBES),
            _cost(),
            config=_config(),
            ledger=ledger,
        )


def test_widened_state_budget_is_rejected_even_with_empty_ledger(budget_ledger) -> None:
    ledger, _ = budget_ledger
    values = initial_budget_state(_config()).model_dump(mode="python")
    values["initial_model_tokens"] = 10**12
    values["remaining_model_tokens"] = 10**12

    with pytest.raises(BudgetAdmissionError, match="versioned policy"):
        reserve_probe_budget(
            _state(budget=BudgetState(**values)),
            _action(0),
            _cost(),
            config=_config(),
            ledger=ledger,
        )


@pytest.mark.parametrize("config", (_config(), _v2_config()))
def test_probe_cost_cannot_charge_model_budget_in_any_policy_version(
    budget_ledger, config: AdaptivePolicyConfig
) -> None:
    ledger, _ = budget_ledger

    with pytest.raises(BudgetAdmissionError, match="probe costs"):
        reserve_probe_budget(
            _state(budget=initial_budget_state(config)),
            _action(0),
            _cost(model_calls=1),
            config=config,
            ledger=ledger,
        )


def test_probe_result_model_copy_cannot_bypass_probe_only_cost(budget_ledger) -> None:
    ledger, _ = budget_ledger
    state = _state()
    action = _action(0)
    reservation = reserve_probe_budget(
        state, action, _cost(), config=_config(), ledger=ledger
    )
    result = _failed_result(action).model_copy(update={"cost": _cost(model_tokens=1)})

    with pytest.raises(ValueError, match="strict contract|probe costs"):
        reconcile_probe_cost(reservation, result)


@pytest.mark.parametrize("config", (_config(), _v2_config()))
def test_direct_probe_ledger_rejects_model_cost_forgery_before_write(
    budget_ledger, config: AdaptivePolicyConfig
) -> None:
    ledger, _ = budget_ledger
    state = _state(budget=initial_budget_state(config))
    action = _action(0)
    reservation = reserve_probe_budget(
        state, action, _cost(), config=config, ledger=ledger
    )
    forged_reservation = reservation.model_copy(
        update={"maximum_cost": _cost(model_calls=1)}
    )
    forged_reservation = forged_reservation.model_copy(
        update={
            "reservation_digest": _probe_policy._reservation_digest(forged_reservation)
        }
    )

    with pytest.raises(ValueError, match="probe costs"):
        BudgetReservation.model_validate(
            forged_reservation.model_dump(mode="python", round_trip=True)
        )
    with pytest.raises(BudgetAdmissionError, match="budget reservation is invalid"):
        ledger.record_reservation(forged_reservation)
    assert ledger.load_records(RUN_ID, INCARNATION)[0].reservation == reservation


@pytest.mark.parametrize(
    "config",
    (
        pytest.param(_config(), id="policy-v1"),
        pytest.param(_v2_config(), id="policy-v2"),
    ),
)
def test_probe_ledger_load_rejects_canonical_model_cost_forgery(
    budget_ledger, config: AdaptivePolicyConfig
) -> None:
    ledger, _ = budget_ledger
    state = _state(budget=initial_budget_state(config))
    action = _action(0)
    reservation = reserve_probe_budget(
        state, action, _cost(), config=config, ledger=ledger
    )
    forged_reservation = reservation.model_copy(
        update={"maximum_cost": _cost(model_tokens=1)}
    )
    forged_reservation = forged_reservation.model_copy(
        update={
            "reservation_digest": _probe_policy._reservation_digest(forged_reservation)
        }
    )
    with ledger._connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DROP TRIGGER adaptive_budget_events_no_update")
        connection.execute(
            """
            UPDATE adaptive_budget_events
            SET payload = ?, identity_digest = ?
            WHERE run_id = ? AND run_incarnation = ?
              AND revision = ? AND phase = 'reservation'
            """,
            (
                canonical_json_bytes(forged_reservation),
                forged_reservation.reservation_digest,
                reservation.run_id,
                reservation.run_incarnation,
                reservation.revision,
            ),
        )

    with pytest.raises(BudgetAdmissionError, match="event payload"):
        ledger.load_records(RUN_ID, INCARNATION)


def test_direct_probe_reconciliation_rejects_model_cost_forgery(budget_ledger) -> None:
    ledger, _ = budget_ledger
    state = _state()
    action = _action(0)
    reservation = reserve_probe_budget(
        state, action, _cost(), config=_config(), ledger=ledger
    )
    assert ledger.claim_execution(reservation, "owner", now_ns=1)
    result = ledger.record_result(
        reservation,
        _failed_result(action),
        owner_token="owner",
    )
    reconciliation = reconcile_probe_cost(reservation, result)
    forged_reconciliation = reconciliation.model_copy(
        update={"actual_cost": _cost(model_tokens=1)}
    )
    forged_reconciliation = forged_reconciliation.model_copy(
        update={
            "reconciliation_digest": _probe_policy._reconciliation_digest(
                forged_reconciliation
            )
        }
    )

    with pytest.raises(ValueError, match="probe costs"):
        BudgetReconciliation.model_validate(
            forged_reconciliation.model_dump(mode="python", round_trip=True)
        )
    with pytest.raises(BudgetAdmissionError, match="budget reconciliation is invalid"):
        ledger.record_reconciliation(forged_reconciliation, result)
    assert ledger.load_records(RUN_ID, INCARNATION)[0].reconciliation is None


def test_sample_rows_limit_is_exact(budget_ledger) -> None:
    ledger, _ = budget_ledger
    exact = _action(
        0,
        kind=ResearchActionKind.SAMPLE_ROWS,
        parameters=(("limit", MAX_SAMPLE_ROWS),),
    )
    assert reserve_probe_budget(
        _state(),
        exact,
        _cost(rows=MAX_SAMPLE_ROWS),
        config=_config(),
        ledger=ledger,
    )


def test_loaded_production_policy_admits_generic_sample_rows_limit_50(
    budget_ledger,
) -> None:
    ledger, _ = budget_ledger
    config = load_adaptive_policy_config()
    action = _action(
        0,
        kind=ResearchActionKind.SAMPLE_ROWS,
        parameters=(("limit", 50),),
    )

    assert reserve_probe_budget(
        _state(budget=initial_budget_state(config)),
        action,
        _cost(rows=50),
        config=config,
        ledger=ledger,
    )


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (TimeoutError("slow"), ProbeStatus.TIMED_OUT),
        (RuntimeError("broken"), ProbeStatus.FAILED),
        (asyncio.CancelledError(), ProbeStatus.CANCELLED),
    ],
)
def test_execute_boundary_returns_and_reconciles_caught_failures(
    tmp_path: Path,
    error: BaseException,
    status: ProbeStatus,
) -> None:
    ledger = AdaptiveBudgetLedger(tmp_path / f"{status.value}.sqlite")

    def execute(_: BudgetReservation) -> ProbeResult:
        raise error

    try:
        result, reconciliation = execute_probe_with_budget(
            _state(),
            _action(0),
            _cost(wall_clock_ms=10, db_probe_ms=0),
            execute,
            config=_config(),
            ledger=ledger,
            monotonic_ns=_clock(0, 5_000_000),
            utc_now=_utc_clock(NOW, NOW + timedelta(milliseconds=5)),
        )
        assert result.status is status
        assert result.cost.wall_clock_ms == 5
        assert reconciliation.actual_cost == result.cost
        assert reconciliation.budget_after.used_wall_clock_ms == 5
        assert (
            ledger.load_records(RUN_ID, INCARNATION)[0].reconciliation == reconciliation
        )
    finally:
        ledger.close()


def test_typed_failure_preserves_actual_non_wall_cost(budget_ledger) -> None:
    ledger, _ = budget_ledger
    actual = _cost(
        wall_clock_ms=2,
        db_probe_ms=7,
        rows=2,
        bytes_=3,
    )

    def execute(_: BudgetReservation) -> ProbeResult:
        raise ProbeExecutionFailure(
            status=ProbeStatus.TIMED_OUT,
            actual_cost=actual,
            failure_code="db_timeout",
            summary="database probe timed out",
        )

    result, reconciliation = execute_probe_with_budget(
        _state(),
        _action(0),
        _cost(
            wall_clock_ms=10,
            db_probe_ms=10,
            rows=2,
            bytes_=3,
        ),
        execute,
        config=_config(),
        ledger=ledger,
        monotonic_ns=_clock(0, 5_000_000),
        utc_now=_utc_clock(NOW, NOW + timedelta(milliseconds=5)),
    )

    assert result.cost == _cost(
        wall_clock_ms=5,
        db_probe_ms=7,
        rows=2,
        bytes_=3,
    )
    assert reconciliation.actual_cost == result.cost


def test_crash_after_reservation_retries_once_and_completed_retry_is_read_only(
    tmp_path: Path,
) -> None:
    path = tmp_path / "crash.sqlite"
    initial_ledger = AdaptiveBudgetLedger(path)
    state = _state()
    action = _action(0)
    reserve_probe_budget(
        state,
        action,
        _cost(wall_clock_ms=10, db_probe_ms=0),
        config=_config(),
        ledger=initial_ledger,
    )
    initial_ledger.close()

    ledger = AdaptiveBudgetLedger(path)
    calls = 0

    def execute(_: BudgetReservation) -> ProbeResult:
        nonlocal calls
        calls += 1
        raise TimeoutError("lost process before callback result was journaled")

    try:
        first = execute_probe_with_budget(
            state,
            action,
            _cost(wall_clock_ms=10, db_probe_ms=0),
            execute,
            config=_config(),
            ledger=ledger,
            monotonic_ns=_clock(0, 3_000_000),
            utc_now=_utc_clock(NOW, NOW + timedelta(milliseconds=3)),
        )
        second = execute_probe_with_budget(
            state,
            action,
            _cost(wall_clock_ms=10, db_probe_ms=0),
            execute,
            config=_config(),
            ledger=ledger,
        )
        assert second == first
        assert calls == 1
    finally:
        ledger.close()


def test_double_reconcile_is_one_append_and_one_charge(budget_ledger) -> None:
    ledger, path = budget_ledger
    state = _state()
    action = _action(0)
    reservation = reserve_probe_budget(
        state,
        action,
        _cost(wall_clock_ms=10, db_probe_ms=10),
        config=_config(),
        ledger=ledger,
    )
    result = _failed_result(action, cost=_cost(wall_clock_ms=5, db_probe_ms=4))
    assert ledger.claim_execution(reservation, "double-owner", now_ns=0)
    result = ledger.record_result(
        reservation,
        result,
        owner_token="double-owner",
    )
    reconciliation = reconcile_probe_cost(reservation, result)

    ledger.record_reconciliation(reconciliation, result)
    ledger.record_reconciliation(reconciliation, result)

    records = ledger.load_records(RUN_ID, INCARNATION)
    assert len(records) == 1
    assert records[0].reconciliation == reconciliation
    assert records[0].reconciliation.budget_after.used_wall_clock_ms == 5
    with sqlite3.connect(path) as connection:
        phases = connection.execute(
            """
            SELECT phase FROM adaptive_budget_events
            WHERE run_id = ? AND run_incarnation = ?
            ORDER BY CASE phase
                WHEN 'reservation' THEN 0
                WHEN 'result' THEN 1
                ELSE 2
            END
            """,
            (RUN_ID, INCARNATION),
        ).fetchall()
    assert phases == [("reservation",), ("result",), ("reconciliation",)]


def test_overrun_persists_once_and_exhausts_next_admission(budget_ledger) -> None:
    ledger, _ = budget_ledger
    state = _state()
    action = _action(0)
    calls = 0

    def execute(_: BudgetReservation) -> ProbeResult:
        nonlocal calls
        calls += 1
        return _failed_result(action, cost=_cost(wall_clock_ms=0, db_probe_ms=4))

    first = execute_probe_with_budget(
        state,
        action,
        _cost(wall_clock_ms=10, db_probe_ms=10),
        execute,
        config=_config(),
        ledger=ledger,
        monotonic_ns=_clock(0, 11_000_000),
        utc_now=_utc_clock(NOW),
    )
    second = execute_probe_with_budget(
        state,
        action,
        _cost(wall_clock_ms=10, db_probe_ms=10),
        execute,
        config=_config(),
        ledger=ledger,
    )

    result, reconciliation = first
    assert second == first
    assert calls == 1
    assert result.cost.wall_clock_ms == 11
    assert reconciliation.actual_cost.wall_clock_ms == 11
    assert reconciliation.charged_cost.wall_clock_ms == 11
    assert reconciliation.overrun_cost.wall_clock_ms == 1
    assert reconciliation.budget_exhausted is True
    assert len(ledger.load_records(RUN_ID, INCARNATION)) == 1

    advanced = apply_research_transition(
        state,
        action,
        budget_state=reconciliation.budget_after,
    ).state
    with pytest.raises(BudgetExhaustedError, match="overrun"):
        reserve_probe_budget(
            advanced,
            _action(1),
            _cost(),
            config=_config(),
            ledger=ledger,
        )


def test_probe_ledger_is_sparse_across_semantic_commit_revision(budget_ledger) -> None:
    ledger, _ = budget_ledger
    semantic = ResearchAction(
        action_id="semantic-0",
        kind=ResearchActionKind.SEMANTIC_COMMIT,
        hypothesis_id=None,
        target=None,
        parameters=(),
        action_digest=canonical_action_digest(
            kind=ResearchActionKind.SEMANTIC_COMMIT,
            hypothesis_id=None,
            target=None,
            parameters=(),
            expected_revision=0,
        ),
        expected_revision=0,
    )
    state = _state(history=(semantic,))

    reservation = reserve_probe_budget(
        state,
        _action(1, action_id="probe-1"),
        _cost(),
        config=_config(),
        ledger=ledger,
    )

    assert reservation.revision == 1
    assert tuple(
        record.reservation.revision
        for record in ledger.load_records(RUN_ID, INCARNATION)
    ) == (1,)
