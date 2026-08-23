"""Production boundaries for one targeted solver-to-research turn."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path
import time

from custom_tools.text_to_sql.adaptive.data_probes import DataProbeRuntime
from custom_tools.text_to_sql.adaptive.decision_resolver import (
    execute_resolved_research_decision,
    resolve_research_decision,
)
from custom_tools.text_to_sql.adaptive.models import (
    BindingStatus,
    EvidenceCost,
    ResearchActionKind,
    ResearchState,
)
from custom_tools.text_to_sql.adaptive.policy import (
    BudgetAdmissionError,
    execute_model_call_with_budget_async,
    reserve_probe_budget,
)
from custom_tools.text_to_sql.adaptive.production_research import (
    assemble_production_research,
    stable_schema_research_model_identity,
)
from custom_tools.text_to_sql.adaptive.research_decision import ResearchDecisionV1
from custom_tools.text_to_sql.adaptive.research_loop import (
    _model_call_id,
    _state_with_reconciled_model_budget,
    run_research_loop,
)
from custom_tools.text_to_sql.schema_memory import SchemaMemoryManager
from custom_tools.text_to_sql.adaptive.research_reentry import (
    _research_context,
    _trusted_targets,
    run_targeted_research_reentry,
)
from custom_tools.text_to_sql.adaptive.schema_probes import (
    SchemaProbeBudgetRuntime,
    SchemaProbeRuntime,
)
from custom_tools.text_to_sql.adaptive.schema_research_agent import (
    SchemaResearchDecisionAdapter,
    load_schema_research_agent_profile,
)
from custom_tools.text_to_sql.adaptive.semantic_reducer import (
    commit_semantic_turn,
)
from custom_tools.text_to_sql.adaptive.serialization import canonical_digest
from custom_tools.text_to_sql.adaptive.tool_registry import (
    AdaptiveResearchToolContext,
    AdaptiveResearchToolRegistry,
)
from workflow.deadline import execute_step_attempt

from .adaptive_budget_ledger import EXECUTION_CLAIM_LEASE_NS
from ._text_to_sql_reentry_recovery import (
    build_prepared_targeted_reentry_commit,
)


class _CapturedSchemaLoader:
    def load_scoped_schema(self, *_args):
        raise RuntimeError("targeted research cannot reload captured schema")


class _TargetedProbeBudgetFactory:
    def __init__(self, runtime: object, research: ResearchState, request: object) -> None:
        self.runtime = runtime
        self.store_base_research = research
        self.research = research
        self.resolved = None
        self.solver_admission = None
        self.plan = None
        self.request = request

    def bind(self, resolved: object) -> None:
        self.resolved = resolved

    def bind_solver_admission(self, state: object, record: object) -> None:
        self.solver_admission = (state, record)

    def __call__(self, kind, target, parameters):
        resolved = self.resolved
        action = getattr(getattr(resolved, "admission", None), "action", None)
        invocation = getattr(resolved, "invocation", None)
        if action is None or invocation is None:
            raise RuntimeError("targeted probe has no bound admission")
        if (
            action.kind is not kind
            or action.target != target
            or action.parameters != parameters
        ):
            raise RuntimeError("targeted probe differs from its admission")
        solver_admission = self.solver_admission
        if solver_admission is None:
            raise RuntimeError("targeted probe has no durable solver admission")
        remaining = self.research.budget_state
        budget = SchemaProbeBudgetRuntime(
            state=self.research,
            action=action,
            maximum_cost=EvidenceCost(
                wall_clock_ms=remaining.remaining_wall_clock_ms,
                model_calls=0,
                model_tokens=0,
                db_probe_ms=remaining.remaining_db_probe_ms,
                rows=(
                    min(remaining.remaining_rows, dict(action.parameters)["limit"])
                    if action.kind is ResearchActionKind.SAMPLE_ROWS
                    else remaining.remaining_rows
                ),
                bytes=remaining.remaining_bytes,
            ),
            config=self.runtime.verified_research_policy,
            ledger=self.runtime.budget_ledger,
            invocation_id=invocation.invocation_id,
        )
        reservation = reserve_probe_budget(
            budget.state,
            budget.action,
            budget.maximum_cost,
            config=budget.config,
            ledger=budget.ledger,
        )
        admitted_state, reentry_record = solver_admission
        plan = build_prepared_targeted_reentry_commit(
            run_id=self.research.run_id,
            run_incarnation=self.research.run_incarnation,
            research_reentry_id=reentry_record.research_reentry_id,
            missing_evidence_request_id=(
                reentry_record.missing_evidence_request_id
            ),
            source_id=reentry_record.source_id,
            ordinal=reentry_record.ordinal,
            base_solver_revision=admitted_state.revision,
            solver_admission_digest=canonical_digest(admitted_state),
            store_base_research_revision=self.store_base_research.revision,
            store_base_research_digest=canonical_digest(self.store_base_research),
            projected_research=self.research,
            projected_research_digest=canonical_digest(self.research),
            action=action,
            hypotheses=resolved.admission.hypotheses,
            bindings=_semantic_repair_bindings(
                resolved.admission.bindings,
                self.research,
                self.request,
            ),
            join_candidates=resolved.admission.join_candidates,
            invocation_id=invocation.invocation_id,
            reservation_digest=reservation.reservation_digest,
            policy_digest=reservation.policy_digest,
            schema_namespace_version=reservation.schema_namespace_version,
        )
        self.runtime.research_state_store.prepare_targeted_reentry_commit(plan)
        self.plan = plan
        return budget


def build_production_reentry_boundary(
    runtime: object,
    *,
    table_namespace: str,
    model: Callable[[str], Awaitable[str | bytes]],
):
    profile = load_schema_research_agent_profile()
    adapter = SchemaResearchDecisionAdapter(profile)

    async def reenter(
        solver_state,
        research_state,
        missing_evidence_request_id,
        *,
        requirements,
        freshness_context,
        commit_solver_admission,
        id_factory,
        resume_admitted=False,
    ):
        from custom_tools.text_to_sql.adaptive.semantic_coverage import (
            validate_coverage_inputs,
        )
        from ._text_to_sql_document_authority import live_document_freshness_context

        request = next(
            item
            for item in solver_state.missing_evidence_requests
            if item.missing_evidence_request_id == missing_evidence_request_id
        )
        async def continue_research(state, repair_request):
            return await _continue_production_research(
                runtime,
                table_namespace,
                model,
                profile,
                state,
                repair_request,
            )
        if resume_admitted:
            outcome = await run_targeted_research_reentry(
                solver_state,
                research_state,
                missing_evidence_request_id,
                requirements=requirements,
                freshness_context=freshness_context,
                loaded_schema=runtime.loaded_schema,
                registry=object(),
                decision_model_type=ResearchDecisionV1,
                propose_decision=lambda **_kwargs: None,
                resolve_decision=lambda *_args, **_kwargs: None,
                execute_probe=lambda *_args, **_kwargs: None,
                commit_research_turn=lambda *_args, **_kwargs: None,
                deadline=runtime.deadline,
                is_cancelled=runtime.is_cancelled,
                id_factory=id_factory,
                continue_research=continue_research,
                resume_admitted=True,
            )
            return outcome
        freshness_context = live_document_freshness_context(runtime, research_state)
        requirements = validate_coverage_inputs(
            research_state,
            freshness_context,
            research_state.run_id,
            research_state.run_incarnation,
        )
        factory = _TargetedProbeBudgetFactory(runtime, research_state, request)
        registry = _registry(runtime, table_namespace, factory)

        def commit_admission(transition):
            committed = commit_solver_admission(transition)
            factory.bind_solver_admission(committed, transition.record)
            return committed

        async def propose_decision(*, task, research_context):
            captured = None

            async def call(_reservation):
                nonlocal captured

                async def invoke(_context):
                    return await adapter.propose_with_usage(
                        model,
                        task=task,
                        research_context=research_context,
                    )

                captured, usage = await execute_step_attempt(
                    "targeted schema research",
                    invoke,
                    None,
                    attempt_timeout=None,
                    deadline=runtime.deadline,
                )
                return usage

            limits = runtime.verified_research_policy.model_budget
            if limits is None:
                raise BudgetAdmissionError("targeted research has no model budget")
            await execute_model_call_with_budget_async(
                research_state.run_id,
                research_state.run_incarnation,
                _model_call_id(research_state, 0),
                canonical_digest(
                    {
                        "research_context": research_context,
                        "state": research_state.model_dump(mode="json", by_alias=True),
                        "task": task,
                    }
                ),
                stable_schema_research_model_identity(profile.model),
                limits.input_tokens_per_call,
                limits.output_tokens_per_call,
                call,
                config=runtime.verified_research_policy,
                ledger=runtime.budget_ledger,
            )
            if captured is None:
                raise BudgetAdmissionError(
                    "targeted research model replay has no durable decision"
                )
            return captured

        def resolve(state, decision, **kwargs):
            projected = _state_with_reconciled_model_budget(
                state,
                runtime.budget_ledger,
                runtime.verified_research_policy,
            )
            factory.research = projected
            resolved = resolve_research_decision(projected, decision, **kwargs)
            factory.bind(resolved)
            return resolved

        def commit(admission, *, probe_result):
            records = runtime.budget_ledger.load_records(
                admission.state.run_id,
                admission.state.run_incarnation,
            )
            action = admission.action
            if action is None:
                raise RuntimeError("targeted probe has no admitted action")
            matches = tuple(
                record
                for record in records
                if record.reservation.revision == admission.state.revision
                and record.reservation.action_digest == action.action_digest
            )
            if len(matches) != 1:
                raise RuntimeError("targeted probe budget record is missing")
            record = matches[0]
            if (
                record.reconciliation is None
            ):
                raise RuntimeError("targeted probe budget is not reconciled")
            committed = commit_semantic_turn(
                replace(
                    admission,
                    bindings=_semantic_repair_bindings(
                        admission.bindings,
                        admission.state,
                        request,
                    ),
                    budget_state=record.reconciliation.budget_after,
                ),
                probe_result=probe_result,
            )
            plan = factory.plan
            if plan is None:
                raise RuntimeError("targeted probe has no prepared commit plan")
            runtime.research_state_store.commit_prepared_targeted_reentry(
                plan,
                committed.state,
            )
            return committed

        outcome = await run_targeted_research_reentry(
            solver_state,
            research_state,
            missing_evidence_request_id,
            requirements=requirements,
            freshness_context=freshness_context,
            loaded_schema=runtime.loaded_schema,
            registry=registry,
            decision_model_type=ResearchDecisionV1,
            propose_decision=propose_decision,
            resolve_decision=resolve,
            execute_probe=execute_resolved_research_decision,
            commit_research_turn=commit,
            deadline=runtime.deadline,
            is_cancelled=runtime.is_cancelled,
            id_factory=id_factory,
            commit_solver_admission=commit_admission,
            continue_research=continue_research,
        )
        projected = _state_with_reconciled_model_budget(
            outcome.research_state,
            runtime.budget_ledger,
            runtime.verified_research_policy,
        )
        freshness_context = live_document_freshness_context(runtime, projected)
        requirements = validate_coverage_inputs(
            projected,
            freshness_context,
            projected.run_id,
            projected.run_incarnation,
        )
        return replace(
            outcome,
            research_state=projected,
            freshness_context=freshness_context,
            requirements=requirements,
        )

    return reenter


async def _continue_production_research(
    runtime,
    table_namespace,
    model,
    profile,
    state,
    repair_request,
):
    terms = tuple(
        dict.fromkeys(
            term
            for item in state.query_spec.semantic_items
            if item.source_id == repair_request.source_id
            for term in (item.source_text, item.normalized_meaning)
            if isinstance(term, str) and term.strip()
        )
    )
    memory = SchemaMemoryManager(Path(__file__).resolve().parents[1])
    assembly = assemble_production_research(
        initial_state=state,
        query=repair_request.question,
        loaded_schema=runtime.loaded_schema,
        semantic_table_hints=tuple(
            memory.find_semantic_relevant_tables(
                terms,
                namespace=runtime.loaded_schema.namespace,
            )
        ),
        verified_probe_fact_hints=tuple(
            memory.find_verified_probe_facts(terms, runtime.loaded_schema.namespace)
        ),
        documents=runtime.document_snapshot,
        dsn=runtime.dsn,
        scope=runtime.loaded_schema.namespace.scope,
        table_namespace=table_namespace,
        model=model,
        model_identity=stable_schema_research_model_identity(profile.model),
        profile=profile,
        state_store=runtime.research_state_store,
        checkpoint_store=runtime.checkpoint_store,
        budget_ledger=runtime.budget_ledger,
        policy=runtime.verified_research_policy,
        deadline=runtime.deadline,
        is_cancelled=runtime.is_cancelled,
        semantic_repair_continuation=True,
    )
    result = await run_research_loop(**assembly.loop_arguments())
    memory.save_verified_probe_facts(runtime.loaded_schema.namespace, result.final_state)
    return result


def _semantic_repair_bindings(bindings, state, request):
    if getattr(request, "repair_kind", None) != "semantic_binding_mismatch":
        return bindings
    matches = tuple(
        binding
        for binding in state.bindings
        if binding.binding_id == request.repair_binding_id
        and binding.source_id == request.source_id
        and binding.status is BindingStatus.SUPPORTED
    )
    if len(matches) != 1 or any(
        binding.binding_id == request.repair_binding_id for binding in bindings
    ):
        raise RuntimeError("semantic repair binding is not the exact supported binding")
    return (*bindings, matches[0].model_copy(update={"status": BindingStatus.STALE}))


async def settle_incomplete_reentry_model_call(
    runtime: object,
    solver_state: object,
    research_state: ResearchState,
    requirements: object,
) -> ResearchState:
    """Conservatively close only the exact STARTED targeted model call."""

    ledger = getattr(runtime, "budget_ledger", None)
    policy = getattr(runtime, "verified_research_policy", None)
    if ledger is None or policy is None:
        return research_state
    admitted = tuple(
        record
        for record in getattr(solver_state, "research_reentries", ())
        if record.status.value == "ADMITTED"
    )
    if len(admitted) != 1 or admitted[0] is not solver_state.research_reentries[-1]:
        return research_state
    records = ledger.load_model_records(
        research_state.run_id,
        research_state.run_incarnation,
    )
    outstanding = tuple(record for record in records if record.reconciliation is None)
    if not outstanding:
        return _state_with_reconciled_model_budget(research_state, ledger, policy)
    if len(outstanding) != 1 or outstanding[0].started is None:
        return research_state

    record = outstanding[0]
    reentry = admitted[0]
    request = next(
        (
            item
            for item in solver_state.missing_evidence_requests
            if item.missing_evidence_request_id
            == reentry.missing_evidence_request_id
        ),
        None,
    )
    limits = getattr(policy, "model_budget", None)
    if request is None or limits is None:
        return research_state
    context = _research_context(
        request,
        _trusted_targets(request.source_id, research_state, requirements),
    )
    profile = load_schema_research_agent_profile()
    expected = (
        _model_call_id(research_state, 0),
        canonical_digest(
            {
                "research_context": context,
                "state": research_state.model_dump(mode="json", by_alias=True),
                "task": request.question,
            }
        ),
        stable_schema_research_model_identity(profile.model),
        limits.input_tokens_per_call,
        limits.output_tokens_per_call,
    )
    reservation = record.reservation
    actual = (
        reservation.call_id,
        reservation.request_digest,
        reservation.model_identity,
        reservation.maximum_input_tokens,
        reservation.maximum_output_tokens,
    )
    if actual != expected:
        return research_state

    async def forbid_provider(_reservation):
        raise RuntimeError("STARTED replay must not invoke the model provider")

    await execute_model_call_with_budget_async(
        research_state.run_id,
        research_state.run_incarnation,
        *expected,
        forbid_provider,
        config=policy,
        ledger=ledger,
        claim_now_ns=lambda: time.time_ns() + EXECUTION_CLAIM_LEASE_NS + 1,
    )
    return _state_with_reconciled_model_budget(research_state, ledger, policy)


def _registry(runtime, table_namespace, budget_factory):
    loaded = runtime.loaded_schema
    loader = _CapturedSchemaLoader()
    common = {
        "loader": loader,
        "dsn": runtime.dsn,
        "scope": loaded.namespace.scope,
        "namespace": loaded.namespace,
        "table_namespace": table_namespace,
        "deadline": runtime.deadline,
        "loaded_schema": loaded,
    }
    return AdaptiveResearchToolRegistry(
        AdaptiveResearchToolContext(
            schema_runtime=SchemaProbeRuntime(
                **common,
                documents=runtime.document_snapshot,
            ),
            data_runtime=DataProbeRuntime(**common),
            budget_factory=budget_factory,
        )
    )


__all__ = (
    "build_production_reentry_boundary",
    "settle_incomplete_reentry_model_call",
)
