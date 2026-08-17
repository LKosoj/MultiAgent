"""One-shot dispatch, recovery, raw SQL, and tamper tests."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

import pytest

from custom_tools.text_to_sql.adaptive.decision_resolver import (
    DecisionAlreadyExecutedError,
    DecisionExecutionError,
    execute_resolved_research_decision,
    resolve_research_decision,
)
from custom_tools.text_to_sql.adaptive.models import EvidenceCost, ResearchActionKind
from custom_tools.text_to_sql.adaptive.probes import ProbeStatus, build_probe_result
from custom_tools.text_to_sql.adaptive.tool_registry import (
    AdaptiveResearchToolContext,
    AdaptiveResearchToolRegistry,
)
from tests.text_to_sql_decision_resolver_helpers import (
    NOW,
    freshness as _freshness,
    make_registry as _registry,
    make_state as _state,
    normalized_failure as _normalized_failure,
    resolve as _resolve,
    resolved_stop as _resolved_stop,
    schema as _schema,
    tool_decision as _tool_decision,
)


def test_executes_exactly_once_and_round_trips_probe_result_losslessly() -> None:
    loaded, namespace = _schema()
    registry = _registry(namespace)
    resolved = _resolve(
        _tool_decision("inspect_table", {"table": "public.orders"}),
        loaded=loaded,
        namespace=namespace,
        registry=registry,
    )
    registry.adapter.result = _normalized_failure(resolved)

    result = execute_resolved_research_decision(resolved, registry)

    assert result.status is ProbeStatus.FAILED
    assert registry.resolve_calls == 1
    assert registry.adapter.execute_calls == 1
    assert registry.adapter.recover_calls == 0
    with pytest.raises(DecisionAlreadyExecutedError):
        execute_resolved_research_decision(resolved, registry)
    assert registry.resolve_calls == 1
    assert registry.adapter.execute_calls == 1


@pytest.mark.parametrize("reason", ["complete", "ambiguous", "unsupported"])
def test_stop_is_inert_exactly_once_sequentially(reason: str) -> None:
    resolved, registry = _resolved_stop(reason)

    assert execute_resolved_research_decision(resolved, registry) is None
    with pytest.raises(DecisionAlreadyExecutedError):
        execute_resolved_research_decision(resolved, registry)

    assert registry.resolve_calls == 0
    assert registry.adapter.execute_calls == 0
    assert registry.adapter.recover_calls == 0


@pytest.mark.parametrize("reason", ["complete", "ambiguous", "unsupported"])
def test_stop_is_inert_exactly_once_for_two_concurrent_callers(reason: str) -> None:
    resolved, registry = _resolved_stop(reason)
    barrier = Barrier(2)

    def call() -> str:
        barrier.wait()
        try:
            result = execute_resolved_research_decision(resolved, registry)
        except DecisionAlreadyExecutedError:
            return "rejected"
        assert result is None
        return "accepted"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(lambda _index: call(), range(2)))

    assert sorted(outcomes) == ["accepted", "rejected"]
    assert registry.resolve_calls == 0
    assert registry.adapter.execute_calls == 0


def test_existing_registry_rederives_the_same_claim_once(monkeypatch) -> None:
    from custom_tools.text_to_sql.adaptive import probes

    loaded, namespace = _schema()
    state = _state(namespace)
    box: dict[str, object] = {"budget_calls": 0, "probe_calls": 0}

    def make_budget(kind, target, parameters):
        box["budget_calls"] = int(box["budget_calls"]) + 1
        resolved = box["resolved"]
        action = resolved.admission.action
        invocation = resolved.invocation
        assert action is not None and invocation is not None
        assert (kind, target, parameters) == (
            action.kind,
            action.target,
            action.parameters,
        )
        return SimpleNamespace(
            state=resolved.admission.state,
            action=action,
            invocation_id=invocation.invocation_id,
            maximum_cost=object(),
            config=object(),
            ledger=object(),
        )

    base_registry = _registry(namespace)
    registry = AdaptiveResearchToolRegistry(
        AdaptiveResearchToolContext(
            schema_runtime=base_registry.context.schema_runtime,
            data_runtime=base_registry.context.data_runtime,
            budget_factory=make_budget,
        )
    )
    resolved = resolve_research_decision(
        state,
        _tool_decision("inspect_table", {"table": "public.orders"}),
        loaded_schema=loaded,
        freshness_context=_freshness(state),
        registry=registry,
    )
    box["resolved"] = resolved

    def inspect_table(_target, *, runtime, budget):
        box["probe_calls"] = int(box["probe_calls"]) + 1
        action = budget.action
        return build_probe_result(
            run_id=budget.state.run_id,
            run_incarnation=budget.state.run_incarnation,
            revision=budget.state.revision,
            schema_namespace_version=budget.state.schema_namespace_version,
            invocation_id=budget.invocation_id,
            action_digest=action.action_digest,
            probe_kind=action.kind,
            status=ProbeStatus.FAILED,
            target=action.target,
            started_at=NOW,
            completed_at=NOW,
            summary="typed failure",
            cost=EvidenceCost(
                wall_clock_ms=0,
                model_calls=0,
                model_tokens=0,
                db_probe_ms=0,
                rows=0,
                bytes=0,
            ),
            row_count=0,
            failure_code="typed_failure",
        )

    monkeypatch.setattr(probes, "inspect_table", inspect_table)

    result = execute_resolved_research_decision(resolved, registry)

    assert result.status is ProbeStatus.FAILED
    assert box["budget_calls"] == 1
    assert box["probe_calls"] == 1
    assert registry.cached_names == ("inspect_table",)


def test_recovers_exactly_once_without_execute() -> None:
    loaded, namespace = _schema()
    registry = _registry(namespace)
    resolved = _resolve(
        _tool_decision("inspect_table", {"table": "public.orders"}),
        loaded=loaded,
        namespace=namespace,
        registry=registry,
    )
    registry.adapter.result = _normalized_failure(resolved)

    result = execute_resolved_research_decision(resolved, registry, recover=True)

    assert result.status is ProbeStatus.FAILED
    assert registry.resolve_calls == 1
    assert registry.adapter.execute_calls == 0
    assert registry.adapter.recover_calls == 1
    with pytest.raises(DecisionAlreadyExecutedError):
        execute_resolved_research_decision(resolved, registry, recover=True)


def test_tampered_invocation_and_swapped_registry_are_rejected_before_dispatch() -> (
    None
):
    loaded, namespace = _schema()
    registry = _registry(namespace)
    resolved = _resolve(
        _tool_decision("inspect_table", {"table": "public.orders"}),
        loaded=loaded,
        namespace=namespace,
        registry=registry,
    )
    assert resolved.invocation is not None
    resolved.invocation.tool_call.arguments["table"] = "public.customers"
    with pytest.raises(DecisionExecutionError, match="identity was changed"):
        execute_resolved_research_decision(resolved, registry)
    assert registry.resolve_calls == 0

    clean = _resolve(
        _tool_decision("inspect_table", {"table": "public.orders"}),
        loaded=loaded,
        namespace=namespace,
        registry=registry,
    )
    with pytest.raises(DecisionExecutionError, match="registry was swapped"):
        execute_resolved_research_decision(clean, _registry(namespace))
    assert registry.resolve_calls == 0


def test_tampered_normalized_result_identity_is_rejected_after_one_dispatch() -> None:
    loaded, namespace = _schema()
    registry = _registry(namespace)
    resolved = _resolve(
        _tool_decision("inspect_table", {"table": "public.orders"}),
        loaded=loaded,
        namespace=namespace,
        registry=registry,
    )
    normalized = _normalized_failure(resolved)
    assert isinstance(normalized.value, dict)
    normalized.value["invocation_id"] = "swapped-invocation"
    registry.adapter.result = normalized

    with pytest.raises(DecisionExecutionError, match="identity does not match"):
        execute_resolved_research_decision(resolved, registry)

    assert registry.resolve_calls == 1
    assert registry.adapter.execute_calls == 1
    with pytest.raises(DecisionAlreadyExecutedError):
        execute_resolved_research_decision(resolved, registry)


def test_raw_unsafe_statement_is_not_pre_rejected_before_claimed_dispatch() -> None:
    loaded, namespace = _schema()
    registry = _registry(namespace)
    decision = _tool_decision(
        "execute_research_probe",
        {"sql": "DELETE FROM public.orders", "parameters": ()},
    )

    resolved = _resolve(
        decision,
        loaded=loaded,
        namespace=namespace,
        registry=registry,
    )
    registry.adapter.result = _normalized_failure(resolved)
    result = execute_resolved_research_decision(resolved, registry)

    assert resolved.admission.action is not None
    assert resolved.admission.action.kind is ResearchActionKind.EXECUTE_PROBE
    assert result.status is ProbeStatus.FAILED
    assert registry.adapter.execute_calls == 1
