"""Targeted one-turn W3 re-entry from a W6 missing-evidence request."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import timedelta
from types import SimpleNamespace

import pytest
from workflow.deadline import DeadlineBudget

import custom_tools.text_to_sql.adaptive.research_reentry as reentry_module
from custom_tools.text_to_sql.adaptive.models import (
    BindingStatus,
    Hypothesis,
    HypothesisStatus,
    PredicateOperator,
    ResearchReentryStatus,
    ResearchStopReason,
    ResearchState,
    SemanticItemKind,
    SemanticItemStatus,
    SolverState,
    SolverStopReason,
    StrictModel,
)
from custom_tools.text_to_sql.adaptive.probes import ProbeStatus
from custom_tools.text_to_sql.adaptive.research_decision import (
    InspectColumnIntent,
    LogicalColumnTarget,
    NewHypothesisProposal,
    ResearchDecisionV1,
    StopRequest,
    ToolIntent,
)
import custom_tools.text_to_sql.adaptive.research_decision as decision_module
from custom_tools.text_to_sql.adaptive.research_reentry import (
    run_targeted_research_reentry,
)
from custom_tools.text_to_sql.adaptive._policy_common import BudgetExhaustedError
from custom_tools.text_to_sql.adaptive.semantic_coverage import validate_coverage_inputs
from custom_tools.text_to_sql.adaptive.solver_loop import apply_solver_proposal
from custom_tools.text_to_sql.adaptive.solver_protocol import (
    MissingEvidenceProposal,
    SolverProposalV1,
)
from custom_tools.text_to_sql.adaptive.models import EvidenceSourceKind
from custom_tools.text_to_sql.adaptive.tool_registry import InspectColumnArguments
from text_to_sql_semantic_checks_helpers import POSTGRES_DSN, ItemSpec, build_case
from text_to_sql_semantic_coverage_helpers import (
    _action as research_action,
    _context,
    _schema_evidence,
)


def _case():
    return build_case(
        "SELECT o.status FROM orders o WHERE o.status = 'active'",
        (
            ItemSpec(
                source_id="status",
                kind=SemanticItemKind.FILTER,
                table="orders",
                column="status",
                operator=PredicateOperator.EQ,
                literal="active",
            ),
        ),
    )


def _stopped(case, *, semantic_repair: bool = False) -> SolverState:
    state = SolverState(
        run_id=case.state.run_id,
        run_incarnation=case.state.run_incarnation,
        revision=1,
        schema_namespace_version=case.state.schema_namespace_version,
        query_spec=case.query_spec,
        sql_candidates=(),
        check_results=(),
        execution_results=(),
        missing_evidence_requests=(),
        action_history=(),
        selected_candidate_id=None,
        stop_reason=None,
    )
    return apply_solver_proposal(
        state,
        SolverProposalV1(
            proposal_version=1,
            proposal=MissingEvidenceProposal(
                proposal_kind="missing_evidence",
                source_id="status",
                question="Which evidence should be refreshed?",
                required_evidence_kind=EvidenceSourceKind.SCHEMA,
                reason="The solver needs one targeted probe.",
                repair_kind=(
                    "semantic_binding_mismatch" if semantic_repair else None
                ),
                repair_binding_id=(
                    case.state.bindings[0].binding_id if semantic_repair else None
                ),
            ),
        ),
        base_revision=state.revision,
        dsn=POSTGRES_DSN,
        table_namespace="main",
        requirements=case.requirements,
        id_factory=iter(("request-1", "solver-action-1")).__next__,
        trusted_semantic_repair=semantic_repair,
    ).state


def _decision() -> ResearchDecisionV1:
    return ResearchDecisionV1.model_validate(
        {
            "decision_version": 1,
            "proposals": (),
            "next": ToolIntent(
                next_kind="tool",
                hypothesis_ref=None,
                intent=InspectColumnIntent(
                    tool_name="inspect_column",
                    arguments=InspectColumnArguments(table="orders", column="status"),
                ),
            ),
        }
    )


def _fresh(case) -> ResearchState:
    evidence = _schema_evidence(
        "fresh-evidence",
        case.requirements.allowed_columns[0],
        revision=2,
    )
    return ResearchState.model_validate(
        {
            **case.state.model_dump(mode="python"),
            "revision": 2,
            "evidence": (*case.state.evidence, evidence),
            "action_history": (
                *case.state.action_history,
                research_action((("revision", 1),), index=1),
            ),
        }
    )


def _semantic_repair_states(case) -> tuple[ResearchState, ResearchState]:
    old = case.state.bindings[0]
    stale = old.model_copy(update={"status": BindingStatus.STALE})
    bridge_item = case.state.query_spec.semantic_items[0].model_copy(
        update={"status": SemanticItemStatus.UNRESOLVED, "binding_ids": ()}
    )
    bridge = ResearchState.model_validate(
        {
            **case.state.model_dump(mode="python"),
            "revision": case.state.revision + 1,
            "query_spec": case.state.query_spec.model_copy(
                update={
                    "revision": case.state.revision + 1,
                    "semantic_items": (bridge_item,),
                }
            ),
            "bindings": (stale,),
            "unresolved_items": (bridge_item.source_id,),
            "action_history": (
                *case.state.action_history,
                research_action(
                    (("revision", case.state.revision),), index=case.state.revision
                ),
            ),
        }
    )
    new_evidence = _schema_evidence(
        "replacement-binding-evidence",
        case.requirements.allowed_columns[0],
        revision=bridge.revision + 1,
    )
    replacement = old.model_copy(
        update={
            "binding_id": "replacement-binding",
            "evidence_ids": (new_evidence.evidence_id,),
        }
    )
    final_item = bridge_item.model_copy(
        update={
            "status": SemanticItemStatus.RESOLVED,
            "binding_ids": (replacement.binding_id,),
        }
    )
    final = ResearchState.model_validate(
        {
            **bridge.model_dump(mode="python"),
            "revision": bridge.revision + 1,
            "query_spec": bridge.query_spec.model_copy(
                update={
                    "revision": bridge.revision + 1,
                    "semantic_items": (final_item,),
                }
            ),
            "evidence": (*bridge.evidence, new_evidence),
            "bindings": (stale, replacement),
            "unresolved_items": (),
            "action_history": (
                *bridge.action_history,
                research_action((("revision", bridge.revision),), index=bridge.revision),
            ),
        }
    )
    return bridge, final


def _request_targets(state: SolverState, *targets) -> SolverState:
    request = state.missing_evidence_requests[0].model_copy(
        update={"candidate_targets": targets}
    )
    return SolverState.model_validate(
        {
            **state.model_dump(mode="python"),
            "missing_evidence_requests": (request,),
        }
    )


def _exhaust_budget(state: ResearchState, name: str) -> ResearchState:
    budget = state.budget_state.model_copy(
        update={f"used_{name}": 1, f"remaining_{name}": 0}
    )
    return ResearchState.model_validate(
        {
            **state.model_dump(mode="python"),
            "budget_state": budget,
        }
    )


def test_fresh_import_has_no_w3_runtime_or_persistence_chain() -> None:
    script = """
import json
import sys
import custom_tools.text_to_sql.adaptive.research_reentry
forbidden = (
    "workflow.adaptive_state_store",
    "sqlite3",
    "custom_tools.text_to_sql.adaptive.controller",
    "custom_tools.text_to_sql.adaptive.decision_resolver",
)
print(json.dumps([name for name in forbidden if name in sys.modules]))
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ("model_calls", "model_tokens"))
async def test_model_budget_exhaustion_stops_before_proposal(name: str) -> None:
    case = _case()
    exhausted = _exhaust_budget(case.state, name)
    calls = {"proposal": 0}

    async def propose(**kwargs):
        calls["proposal"] += 1
        return _decision()

    outcome = await run_targeted_research_reentry(
        _stopped(case),
        exhausted,
        "request-1",
        requirements=validate_coverage_inputs(
            exhausted,
            _context(),
            exhausted.run_id,
            exhausted.run_incarnation,
        ),
        freshness_context=_context(),
        loaded_schema=object(),
        registry=object(),
        decision_model_type=ResearchDecisionV1,
        propose_decision=propose,
        resolve_decision=lambda *args, **kwargs: None,
        execute_probe=lambda *args, **kwargs: None,
        commit_research_turn=lambda *args, **kwargs: None,
        deadline=None,
        is_cancelled=lambda: False,
        id_factory=iter(("reentry-1",)).__next__,
    )

    assert calls["proposal"] == 0
    assert outcome.record.status is ResearchReentryStatus.BUDGET_EXHAUSTED


@pytest.mark.asyncio
async def test_probe_budget_exhaustion_stops_before_execute() -> None:
    case = _case()
    exhausted = _exhaust_budget(case.state, "db_probe_ms")
    calls = {"proposal": 0, "resolve": 0, "execute": 0}

    async def propose(**kwargs):
        calls["proposal"] += 1
        return _decision()

    def resolve(*args, **kwargs):
        calls["resolve"] += 1
        return SimpleNamespace(
            tool_claim=SimpleNamespace(target=case.requirements.allowed_columns[0]),
            admission=object(),
            invocation=object(),
        )

    def execute(*args, **kwargs):
        calls["execute"] += 1
        raise AssertionError("exhausted probe reached execution")

    outcome = await run_targeted_research_reentry(
        _stopped(case),
        exhausted,
        "request-1",
        requirements=validate_coverage_inputs(
            exhausted,
            _context(),
            exhausted.run_id,
            exhausted.run_incarnation,
        ),
        freshness_context=_context(),
        loaded_schema=object(),
        registry=object(),
        decision_model_type=ResearchDecisionV1,
        propose_decision=propose,
        resolve_decision=resolve,
        execute_probe=execute,
        commit_research_turn=lambda *args, **kwargs: None,
        deadline=None,
        is_cancelled=lambda: False,
        id_factory=iter(("reentry-1",)).__next__,
    )

    assert calls == {"proposal": 1, "resolve": 1, "execute": 0}
    assert outcome.record.status is ResearchReentryStatus.BUDGET_EXHAUSTED


@pytest.mark.asyncio
async def test_coordinator_makes_one_decision_resolve_probe_and_w3_commit() -> None:
    case = _case()
    stopped = _stopped(case)
    fresh = _fresh(case)
    target = case.requirements.allowed_columns[0]
    calls = {
        "solver_admission": 0,
        "decision": 0,
        "resolve": 0,
        "probe": 0,
        "commit": 0,
    }

    def commit_solver_admission(transition):
        calls["solver_admission"] += 1
        assert calls["decision"] == 0
        return transition.state

    async def propose(*args, **kwargs):
        calls["decision"] += 1
        return _decision()

    def resolve(*args, **kwargs):
        calls["resolve"] += 1
        return SimpleNamespace(
            tool_claim=SimpleNamespace(target=target),
            admission=object(),
            invocation=object(),
        )

    def probe(*args, **kwargs):
        calls["probe"] += 1
        return SimpleNamespace(status=ProbeStatus.SUCCESS)

    def commit(*args, **kwargs):
        calls["commit"] += 1
        return SimpleNamespace(state=fresh)

    outcome = await run_targeted_research_reentry(
        stopped,
        case.state,
        "request-1",
        requirements=case.requirements,
        freshness_context=_context(),
        loaded_schema=object(),
        registry=object(),
        decision_model_type=ResearchDecisionV1,
        propose_decision=propose,
        resolve_decision=resolve,
        execute_probe=probe,
        commit_research_turn=commit,
        commit_solver_admission=commit_solver_admission,
        deadline=None,
        is_cancelled=lambda: False,
        id_factory=iter(("reentry-1",)).__next__,
    )

    assert calls == {
        "solver_admission": 1,
        "decision": 1,
        "resolve": 1,
        "probe": 1,
        "commit": 1,
    }
    assert outcome.solver_state.stop_reason is None
    assert outcome.solver_state.action_history == stopped.action_history
    assert outcome.research_state.revision == case.state.revision + 1
    assert outcome.record.status is ResearchReentryStatus.COMPLETED


@pytest.mark.asyncio
async def test_semantic_binding_repair_continues_existing_research_until_rebound() -> None:
    from custom_tools.text_to_sql.adaptive.research_loop import ResearchLoopOutcome

    case = _case()
    stopped = _stopped(case, semantic_repair=True)
    bridge, final = _semantic_repair_states(case)
    target = case.requirements.allowed_columns[0]
    continued = []

    async def continue_research(state, request):
        continued.append((state, request))
        assert state == bridge
        assert state.bindings[0].status is BindingStatus.STALE
        assert state.unresolved_items == (request.source_id,)
        return ResearchLoopOutcome(
            final_state=final,
            stop_reason=ResearchStopReason.COMPLETE,
            affected_source_ids=(),
            citation_evidence_ids=tuple(
                evidence.evidence_id for evidence in final.evidence
            ),
            ambiguity=None,
        )

    outcome = await run_targeted_research_reentry(
        stopped,
        case.state,
        "request-1",
        requirements=case.requirements,
        freshness_context=_context(),
        loaded_schema=object(),
        registry=object(),
        decision_model_type=ResearchDecisionV1,
        propose_decision=lambda **_kwargs: _decision(),
        resolve_decision=lambda *_args, **_kwargs: SimpleNamespace(
            tool_claim=SimpleNamespace(target=target),
            admission=object(),
            invocation=object(),
        ),
        execute_probe=lambda *_args, **_kwargs: SimpleNamespace(
            status=ProbeStatus.SUCCESS
        ),
        commit_research_turn=lambda *_args, **_kwargs: SimpleNamespace(state=bridge),
        continue_research=continue_research,
        deadline=None,
        is_cancelled=lambda: False,
        id_factory=iter(("reentry-1",)).__next__,
    )

    assert len(continued) == 1
    assert outcome.record.status is ResearchReentryStatus.COMPLETED
    assert outcome.research_state == final
    assert outcome.solver_state.stop_reason is None


@pytest.mark.asyncio
async def test_reentry_refreshes_freshness_after_durable_probe_commit() -> None:
    case = _case()
    stopped = _stopped(case)
    old_freshness = _context()
    target = case.requirements.allowed_columns[0]
    evidence = _schema_evidence(
        "post-commit-evidence",
        target,
        completed_at=old_freshness.evaluated_at + timedelta(microseconds=1),
        revision=case.state.revision + 1,
    )
    fresh = ResearchState.model_validate(
        {
            **case.state.model_dump(mode="python"),
            "revision": case.state.revision + 1,
            "evidence": (*case.state.evidence, evidence),
            "action_history": (
                *case.state.action_history,
                research_action((("revision", case.state.revision),), index=1),
            ),
        }
    )

    outcome = await run_targeted_research_reentry(
        stopped,
        case.state,
        "request-1",
        requirements=case.requirements,
        freshness_context=old_freshness,
        loaded_schema=object(),
        registry=object(),
        decision_model_type=ResearchDecisionV1,
        propose_decision=lambda **_kwargs: _decision(),
        resolve_decision=lambda *_args, **_kwargs: SimpleNamespace(
            tool_claim=SimpleNamespace(target=target),
            admission=object(),
            invocation=object(),
        ),
        execute_probe=lambda *_args, **_kwargs: SimpleNamespace(
            status=ProbeStatus.SUCCESS
        ),
        commit_research_turn=lambda *_args, **_kwargs: SimpleNamespace(state=fresh),
        deadline=None,
        is_cancelled=lambda: False,
        id_factory=iter(("reentry-1",)).__next__,
    )

    assert evidence.observed_at > old_freshness.evaluated_at
    assert outcome.record.status is ResearchReentryStatus.COMPLETED
    assert outcome.record.evidence_ids == (evidence.evidence_id,)


@pytest.mark.asyncio
async def test_resolved_target_outside_trusted_scope_never_executes() -> None:
    case = _case()
    stopped = _stopped(case)
    calls = {"probe": 0}

    async def propose(*args, **kwargs):
        return _decision()

    def resolve(*args, **kwargs):
        return SimpleNamespace(
            tool_claim=SimpleNamespace(
                target=case.requirements.allowed_columns[0].model_copy(
                    update={"column": "forged"}
                )
            ),
            admission=object(),
            invocation=object(),
        )

    def probe(*args, **kwargs):
        calls["probe"] += 1
        raise AssertionError("forged target reached execution")

    outcome = await run_targeted_research_reentry(
        stopped,
        case.state,
        "request-1",
        requirements=case.requirements,
        freshness_context=_context(),
        loaded_schema=object(),
        registry=object(),
        decision_model_type=ResearchDecisionV1,
        propose_decision=propose,
        resolve_decision=resolve,
        execute_probe=probe,
        commit_research_turn=lambda *args, **kwargs: None,
        deadline=None,
        is_cancelled=lambda: False,
        id_factory=iter(("reentry-1",)).__next__,
    )

    assert calls["probe"] == 0
    assert outcome.record.status is ResearchReentryStatus.PROTOCOL_FAILURE
    assert outcome.solver_state.stop_reason is SolverStopReason.MISSING_EVIDENCE


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_at", (1, 2, 3, 4))
async def test_cancel_is_checked_before_every_external_boundary(
    cancel_at: int,
) -> None:
    case = _case()
    stopped = _stopped(case)
    fresh = _fresh(case)
    target = case.requirements.allowed_columns[0]
    calls = {"boundary": 0, "decision": 0, "resolve": 0, "probe": 0, "commit": 0}

    async def propose(*args, **kwargs):
        calls["decision"] += 1
        return _decision()

    def resolve(*args, **kwargs):
        calls["resolve"] += 1
        return SimpleNamespace(
            tool_claim=SimpleNamespace(target=target),
            admission=object(),
            invocation=object(),
        )

    def probe(*args, **kwargs):
        calls["probe"] += 1
        return SimpleNamespace(status=ProbeStatus.SUCCESS)

    def commit(*args, **kwargs):
        calls["commit"] += 1
        return SimpleNamespace(state=fresh)

    def is_cancelled() -> bool:
        calls["boundary"] += 1
        return calls["boundary"] == cancel_at

    outcome = await run_targeted_research_reentry(
        stopped,
        case.state,
        "request-1",
        requirements=case.requirements,
        freshness_context=_context(),
        loaded_schema=object(),
        registry=object(),
        decision_model_type=ResearchDecisionV1,
        propose_decision=propose,
        resolve_decision=resolve,
        execute_probe=probe,
        commit_research_turn=commit,
        deadline=None,
        is_cancelled=is_cancelled,
        id_factory=iter(("reentry-1",)).__next__,
    )

    assert calls["decision"] == int(cancel_at > 1)
    assert calls["resolve"] == int(cancel_at > 2)
    assert calls["probe"] == int(cancel_at > 3)
    assert calls["commit"] == int(cancel_at > 3)
    assert outcome.record.status is ResearchReentryStatus.CANCELLED
    assert outcome.solver_state.stop_reason is SolverStopReason.MISSING_EVIDENCE


@pytest.mark.asyncio
async def test_stop_instead_of_exact_tool_intent_is_protocol_failure() -> None:
    case = _case()
    stopped = _stopped(case)

    async def propose(*args, **kwargs):
        return ResearchDecisionV1.model_validate(
            {
                "decision_version": 1,
                "proposals": (),
                "next": StopRequest(
                    next_kind="stop",
                    reason="complete",
                    source_ids=(),
                    citation_evidence_ids=(case.state.evidence[0].evidence_id,),
                ),
            }
        )

    outcome = await run_targeted_research_reentry(
        stopped,
        case.state,
        "request-1",
        requirements=case.requirements,
        freshness_context=_context(),
        loaded_schema=object(),
        registry=object(),
        decision_model_type=ResearchDecisionV1,
        propose_decision=propose,
        resolve_decision=lambda *args, **kwargs: None,
        execute_probe=lambda *args, **kwargs: None,
        commit_research_turn=lambda *args, **kwargs: None,
        deadline=None,
        is_cancelled=lambda: False,
        id_factory=iter(("reentry-1",)).__next__,
    )

    assert outcome.record.status is ResearchReentryStatus.PROTOCOL_FAILURE
    assert outcome.research_state == case.state


@pytest.mark.asyncio
async def test_semantic_proposal_is_rejected_before_resolution() -> None:
    case = _case()
    calls = {"resolve": 0}
    proposal = NewHypothesisProposal(
        proposal_type="new_hypothesis",
        proposal_key="proposal:targeted",
        source_ids=("status",),
        claim="A new binding might exist.",
        candidate_targets=(
            LogicalColumnTarget(target_kind="column", table="orders", column="status"),
        ),
        citation_evidence_ids=(case.state.evidence[0].evidence_id,),
    )

    async def propose(*args, **kwargs):
        return ResearchDecisionV1.model_validate(
            {
                **_decision().model_dump(mode="python"),
                "proposals": (proposal,),
            }
        )

    def resolve(*args, **kwargs):
        calls["resolve"] += 1
        raise AssertionError("proposal reached W3 resolution")

    outcome = await run_targeted_research_reentry(
        _stopped(case),
        case.state,
        "request-1",
        requirements=case.requirements,
        freshness_context=_context(),
        loaded_schema=object(),
        registry=object(),
        decision_model_type=ResearchDecisionV1,
        propose_decision=propose,
        resolve_decision=resolve,
        execute_probe=lambda *args, **kwargs: None,
        commit_research_turn=lambda *args, **kwargs: None,
        deadline=None,
        is_cancelled=lambda: False,
        id_factory=iter(("reentry-1",)).__next__,
    )

    assert calls["resolve"] == 0
    assert outcome.record.status is ResearchReentryStatus.PROTOCOL_FAILURE


@pytest.mark.asyncio
async def test_spoofed_decision_type_is_rejected_before_resolution() -> None:
    case = _case()
    calls = {"resolve": 0}
    forged_next_type = type(
        "ToolIntent",
        (),
        {"__module__": "custom_tools.text_to_sql.adaptive.research_decision"},
    )
    forged_decision_type = type(
        "ResearchDecisionV1",
        (),
        {"__module__": "custom_tools.text_to_sql.adaptive.research_decision"},
    )
    forged_decision = forged_decision_type()
    forged_decision.proposals = ()
    forged_decision.next = forged_next_type()

    def resolve(*args, **kwargs):
        calls["resolve"] += 1
        return None

    outcome = await run_targeted_research_reentry(
        _stopped(case),
        case.state,
        "request-1",
        requirements=case.requirements,
        freshness_context=_context(),
        loaded_schema=object(),
        registry=object(),
        decision_model_type=ResearchDecisionV1,
        propose_decision=lambda **kwargs: forged_decision,
        resolve_decision=resolve,
        execute_probe=lambda *args, **kwargs: None,
        commit_research_turn=lambda *args, **kwargs: None,
        deadline=None,
        is_cancelled=lambda: False,
        id_factory=iter(("reentry-1",)).__next__,
    )

    assert calls["resolve"] == 0
    assert outcome.record.status is ResearchReentryStatus.PROTOCOL_FAILURE


@pytest.mark.asyncio
async def test_spoofed_strict_model_decision_is_rejected_before_resolution() -> None:
    case = _case()
    calls = {"resolve": 0}
    forged_next_type = type(
        "ToolIntent",
        (StrictModel,),
        {"__module__": "custom_tools.text_to_sql.adaptive.research_decision"},
    )
    forged_decision_type = type(
        "ResearchDecisionV1",
        (StrictModel,),
        {
            "__module__": "custom_tools.text_to_sql.adaptive.research_decision",
            "__annotations__": {
                "proposals": tuple,
                "next": forged_next_type,
            },
        },
    )
    forged_decision = forged_decision_type(
        proposals=(),
        next=forged_next_type(),
    )

    def resolve(*args, **kwargs):
        calls["resolve"] += 1
        return None

    outcome = await run_targeted_research_reentry(
        _stopped(case),
        case.state,
        "request-1",
        requirements=case.requirements,
        freshness_context=_context(),
        loaded_schema=object(),
        registry=object(),
        decision_model_type=forged_decision_type,
        propose_decision=lambda **kwargs: forged_decision,
        resolve_decision=resolve,
        execute_probe=lambda *args, **kwargs: None,
        commit_research_turn=lambda *args, **kwargs: None,
        deadline=None,
        is_cancelled=lambda: False,
        id_factory=iter(("reentry-1",)).__next__,
    )

    assert calls["resolve"] == 0
    assert outcome.record.status is ResearchReentryStatus.PROTOCOL_FAILURE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tamper_registry", "tamper_module"),
    ((True, False), (False, True), (True, True)),
)
async def test_registry_or_module_tamper_cannot_replace_captured_w3_types(
    monkeypatch: pytest.MonkeyPatch,
    tamper_registry: bool,
    tamper_module: bool,
) -> None:
    from custom_tools.text_to_sql.adaptive import serialization

    case = _case()
    calls = {"proposal": 0, "resolve": 0}
    module_name = "custom_tools.text_to_sql.adaptive.research_decision"
    forged_next_type = type(
        "ToolIntent",
        (StrictModel,),
        {"__module__": module_name},
    )
    forged_decision_type = type(
        "ResearchDecisionV1",
        (StrictModel,),
        {
            "__module__": module_name,
            "__annotations__": {
                "proposals": tuple,
                "next": forged_next_type,
            },
        },
    )
    forged_decision = forged_decision_type(
        proposals=(),
        next=forged_next_type(),
    )
    identity = (module_name, "ResearchDecisionV1")
    if tamper_registry:
        monkeypatch.setitem(
            serialization._INTERNAL_DECODE_MODEL_OBJECTS,
            identity,
            forged_decision_type,
        )
    if tamper_module:
        monkeypatch.setattr(
            decision_module,
            "ResearchDecisionV1",
            forged_decision_type,
        )
        monkeypatch.setattr(decision_module, "ToolIntent", forged_next_type)

    def propose(**kwargs):
        calls["proposal"] += 1
        return forged_decision

    def resolve(*args, **kwargs):
        calls["resolve"] += 1
        return None

    outcome = await run_targeted_research_reentry(
        _stopped(case),
        case.state,
        "request-1",
        requirements=case.requirements,
        freshness_context=_context(),
        loaded_schema=object(),
        registry=object(),
        decision_model_type=forged_decision_type,
        propose_decision=propose,
        resolve_decision=resolve,
        execute_probe=lambda *args, **kwargs: None,
        commit_research_turn=lambda *args, **kwargs: None,
        deadline=None,
        is_cancelled=lambda: False,
        id_factory=iter(("reentry-1",)).__next__,
    )

    assert calls == {"proposal": 0, "resolve": 0}
    assert outcome.record.status is ResearchReentryStatus.PROTOCOL_FAILURE


@pytest.mark.asyncio
async def test_captured_genuine_types_still_succeed_during_external_tamper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_tools.text_to_sql.adaptive import serialization

    case = _case()
    stopped = _stopped(case)
    fresh = _fresh(case)
    target = case.requirements.allowed_columns[0]
    genuine_decision = _decision()
    calls = {"proposal": 0, "resolve": 0, "probe": 0, "commit": 0}
    module_name = "custom_tools.text_to_sql.adaptive.research_decision"
    forged_next_type = type(
        "ToolIntent",
        (StrictModel,),
        {"__module__": module_name},
    )
    forged_type = type(
        "ResearchDecisionV1",
        (StrictModel,),
        {
            "__module__": module_name,
            "__annotations__": {
                "proposals": tuple,
                "next": forged_next_type,
            },
        },
    )
    monkeypatch.setitem(
        serialization._INTERNAL_DECODE_MODEL_OBJECTS,
        (module_name, "ResearchDecisionV1"),
        forged_type,
    )
    monkeypatch.setattr(decision_module, "ResearchDecisionV1", forged_type)
    monkeypatch.setattr(decision_module, "ToolIntent", forged_next_type)

    def propose(**kwargs):
        calls["proposal"] += 1
        return genuine_decision

    def resolve(*args, **kwargs):
        calls["resolve"] += 1
        return SimpleNamespace(
            tool_claim=SimpleNamespace(target=target),
            admission=object(),
            invocation=object(),
        )

    def probe(*args, **kwargs):
        calls["probe"] += 1
        return SimpleNamespace(status=ProbeStatus.SUCCESS)

    def commit(*args, **kwargs):
        calls["commit"] += 1
        return SimpleNamespace(state=fresh)

    outcome = await run_targeted_research_reentry(
        stopped,
        case.state,
        "request-1",
        requirements=case.requirements,
        freshness_context=_context(),
        loaded_schema=object(),
        registry=object(),
        decision_model_type=reentry_module._CANONICAL_RESEARCH_DECISION_TYPE,
        propose_decision=propose,
        resolve_decision=resolve,
        execute_probe=probe,
        commit_research_turn=commit,
        deadline=None,
        is_cancelled=lambda: False,
        id_factory=iter(("reentry-1",)).__next__,
    )

    assert calls == {"proposal": 1, "resolve": 1, "probe": 1, "commit": 1}
    assert outcome.record.status is ResearchReentryStatus.COMPLETED


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ("root_dict", "nested_private"))
async def test_forged_pydantic_internals_are_rejected_before_resolution(
    tamper: str,
) -> None:
    case = _case()
    calls = {"proposal": 0, "resolve": 0}
    decision = _decision()
    if tamper == "root_dict":
        decision.__dict__["hidden"] = "forged"
    else:
        object.__setattr__(
            decision.next,
            "__pydantic_private__",
            {"hidden": "forged"},
        )

    def propose(**kwargs):
        calls["proposal"] += 1
        return decision

    def resolve(*args, **kwargs):
        calls["resolve"] += 1
        return None

    outcome = await run_targeted_research_reentry(
        _stopped(case),
        case.state,
        "request-1",
        requirements=case.requirements,
        freshness_context=_context(),
        loaded_schema=object(),
        registry=object(),
        decision_model_type=ResearchDecisionV1,
        propose_decision=propose,
        resolve_decision=resolve,
        execute_probe=lambda *args, **kwargs: None,
        commit_research_turn=lambda *args, **kwargs: None,
        deadline=None,
        is_cancelled=lambda: False,
        id_factory=iter(("reentry-1",)).__next__,
    )

    assert calls == {"proposal": 1, "resolve": 0}
    assert outcome.record.status is ResearchReentryStatus.PROTOCOL_FAILURE


def test_scope_adds_only_active_same_source_hypothesis_targets() -> None:
    case = _case()
    candidate = case.requirements.allowed_columns[0].model_copy(
        update={"column": "hypothesis_candidate"}
    )
    active = Hypothesis(
        hypothesis_id="hypothesis-1",
        source_ids=("status",),
        claim="The value may live in a related physical column.",
        candidate_targets=(candidate,),
        status=HypothesisStatus.PROPOSED,
        evidence_ids=(),
    )
    research = ResearchState.model_validate(
        {
            **case.state.model_dump(mode="python"),
            "hypotheses": (active,),
        }
    )
    authority = validate_coverage_inputs(
        research,
        _context(),
        research.run_id,
        research.run_incarnation,
    )

    trusted = reentry_module._trusted_targets("status", research, authority)

    assert candidate in trusted
    rejected = active.model_copy(
        update={
            "status": HypothesisStatus.REJECTED,
            "evidence_ids": (case.state.evidence[0].evidence_id,),
        }
    )
    rejected_research = ResearchState.model_validate(
        {
            **research.model_dump(mode="python"),
            "hypotheses": (rejected,),
        }
    )
    rejected_authority = validate_coverage_inputs(
        rejected_research,
        _context(),
        rejected_research.run_id,
        rejected_research.run_incarnation,
    )
    assert candidate not in reentry_module._trusted_targets(
        "status", rejected_research, rejected_authority
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("forged", (False, True))
async def test_request_candidates_are_subset_checks_never_scope_authority(
    forged: bool,
) -> None:
    case = _case()
    target = case.requirements.allowed_columns[0]
    if forged:
        target = target.model_copy(update={"column": "request_only_target"})
    stopped = _request_targets(_stopped(case), target)
    outcome = await run_targeted_research_reentry(
        stopped,
        case.state,
        "request-1",
        requirements=case.requirements,
        freshness_context=_context(),
        loaded_schema=object(),
        registry=object(),
        decision_model_type=ResearchDecisionV1,
        propose_decision=lambda **kwargs: _decision(),
        resolve_decision=lambda *args, **kwargs: None,
        execute_probe=lambda *args, **kwargs: None,
        commit_research_turn=lambda *args, **kwargs: None,
        deadline=None,
        is_cancelled=lambda: True,
        id_factory=iter(("reentry-1",)).__next__,
    )

    assert outcome.record.status is (
        ResearchReentryStatus.PROTOCOL_FAILURE
        if forged
        else ResearchReentryStatus.CANCELLED
    )


@pytest.mark.asyncio
async def test_expired_deadline_terminalizes_before_model_call() -> None:
    case = _case()
    stopped = _stopped(case)
    calls = {"decision": 0}

    async def propose(*args, **kwargs):
        calls["decision"] += 1
        return _decision()

    expired = DeadlineBudget(
        deadline_monotonic=0.0,
        deadline_at_ms=0,
        monotonic=lambda: 1.0,
        wall_time=lambda: 1.0,
    )

    outcome = await run_targeted_research_reentry(
        stopped,
        case.state,
        "request-1",
        requirements=case.requirements,
        freshness_context=_context(),
        loaded_schema=object(),
        registry=object(),
        decision_model_type=ResearchDecisionV1,
        propose_decision=propose,
        resolve_decision=lambda *args, **kwargs: None,
        execute_probe=lambda *args, **kwargs: None,
        commit_research_turn=lambda *args, **kwargs: None,
        deadline=expired,
        is_cancelled=lambda: False,
        id_factory=iter(("reentry-1",)).__next__,
    )

    assert calls["decision"] == 0
    assert outcome.record.status is ResearchReentryStatus.DEADLINE_EXCEEDED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected"),
    (
        (BudgetExhaustedError("no W3 budget"), ResearchReentryStatus.BUDGET_EXHAUSTED),
        (ValueError("bad W3 protocol"), ResearchReentryStatus.PROTOCOL_FAILURE),
    ),
)
async def test_decision_failures_terminalize_with_closed_status(
    failure: Exception,
    expected: ResearchReentryStatus,
) -> None:
    case = _case()

    async def propose(*args, **kwargs):
        raise failure

    outcome = await run_targeted_research_reentry(
        _stopped(case),
        case.state,
        "request-1",
        requirements=case.requirements,
        freshness_context=_context(),
        loaded_schema=object(),
        registry=object(),
        decision_model_type=ResearchDecisionV1,
        propose_decision=propose,
        resolve_decision=lambda *args, **kwargs: None,
        execute_probe=lambda *args, **kwargs: None,
        commit_research_turn=lambda *args, **kwargs: None,
        deadline=None,
        is_cancelled=lambda: False,
        id_factory=iter(("reentry-1",)).__next__,
    )

    assert outcome.record.status is expected
    assert outcome.solver_state.stop_reason is SolverStopReason.MISSING_EVIDENCE


@pytest.mark.asyncio
async def test_tool_exception_terminalizes_without_research_commit() -> None:
    case = _case()

    async def propose(*args, **kwargs):
        return _decision()

    def resolve(*args, **kwargs):
        return SimpleNamespace(
            tool_claim=SimpleNamespace(target=case.requirements.allowed_columns[0]),
            admission=object(),
            invocation=object(),
        )

    def execute(*args, **kwargs):
        raise RuntimeError("tool failed")

    outcome = await run_targeted_research_reentry(
        _stopped(case),
        case.state,
        "request-1",
        requirements=case.requirements,
        freshness_context=_context(),
        loaded_schema=object(),
        registry=object(),
        decision_model_type=ResearchDecisionV1,
        propose_decision=propose,
        resolve_decision=resolve,
        execute_probe=execute,
        commit_research_turn=lambda *args, **kwargs: None,
        deadline=None,
        is_cancelled=lambda: False,
        id_factory=iter(("reentry-1",)).__next__,
    )

    assert outcome.record.status is ResearchReentryStatus.TOOL_FAILURE
    assert outcome.research_state == case.state
