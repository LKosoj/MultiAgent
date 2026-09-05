"""Production boundaries for one targeted solver-to-research turn."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path
import logging
import time

from custom_tools.text_to_sql.adaptive.data_probes import DataProbeRuntime
from custom_tools.text_to_sql.adaptive.decision_resolver import (
    execute_resolved_research_decision,
    resolve_research_decision,
)
from custom_tools.text_to_sql.adaptive.models import (
    BindingStatus,
    DiscriminatorValueBinding,
    EvidenceCost,
    EvidenceSourceKind,
    PhysicalColumnBinding,
    PredicateOperator,
    PredicateRef,
    ResearchActionKind,
    ResearchState,
    SemanticItemKind,
)
from custom_tools.text_to_sql.adaptive.model_budget import ModelTokenUsage
from custom_tools.text_to_sql.adaptive.probes import (
    ProbeResult,
    ProbeStatus,
    read_probe_payload,
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
from custom_tools.text_to_sql.llm_models_config import step_model_name
from workflow.deadline import execute_step_attempt

from .adaptive_budget_ledger import EXECUTION_CLAIM_LEASE_NS
from ._text_to_sql_reentry_recovery import (
    build_prepared_targeted_reentry_commit,
)
from .text_to_sql_typed_research import _research_stop_review_model


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
                        probe_result,
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
    limits = runtime.verified_research_policy.model_budget
    if limits is None:
        raise BudgetAdmissionError("targeted research has no model budget")
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
        approved_semantic_fact_hints=tuple(
            memory.find_approved_semantic_facts(
                terms,
                runtime.loaded_schema.namespace,
            )
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
        # Independent stop-review model: reuse the typed-research JSON-schema
        # route so ResearchStopReview (a flat model without $defs) does not get
        # forced through the ResearchDecisionV1-shaped response_format.
        stop_review_model=_research_stop_review_model(
            step_model_name("research_stop_review"),
            limits.output_tokens_per_call,
            state.run_id,
            input_tokens=limits.input_tokens_per_call,
        ),
    )
    result = await run_research_loop(**assembly.loop_arguments())
    memory.save_verified_probe_facts(runtime.loaded_schema.namespace, result.final_state)
    return result


def _semantic_repair_bindings(
    bindings, state, request, probe_result: ProbeResult | None = None
):
    updated = bindings
    if getattr(request, "repair_kind", None) == "semantic_binding_mismatch":
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
            raise RuntimeError(
                "semantic repair binding is not the exact supported binding"
            )
        updated = (*bindings, matches[0].model_copy(update={"status": BindingStatus.STALE}))
    if (
        probe_result is not None
        and request.required_evidence_kind is EvidenceSourceKind.VALUE_SEARCH
        and probe_result.probe_kind
        in {ResearchActionKind.SEARCH_VALUE, ResearchActionKind.DISTINCT_VALUES}
        and probe_result.status is ProbeStatus.SUCCESS
    ):
        available = {binding.binding_id: binding for binding in state.bindings}
        available.update({binding.binding_id: binding for binding in updated})
        matches = tuple(
            binding
            for binding in available.values()
            if binding.source_id == request.source_id
            and binding.status is BindingStatus.SUPPORTED
            and probe_result.target in binding.columns
        )
        if len(matches) != 1:
            raise RuntimeError("value evidence has no exact supported binding")
        match = matches[0]
        evidence_ids = (*match.evidence_ids, probe_result.invocation_id)
        source_item = next(
            item
            for item in state.query_spec.semantic_items
            if item.source_id == request.source_id
        )
        payload = read_probe_payload(probe_result)
        rows = payload.get("rows") if isinstance(payload, dict) else None
        columns = payload.get("columns") if isinstance(payload, dict) else None
        has_exact_row = (
            isinstance(rows, list)
            and len(rows) == 1
            and isinstance(rows[0], list)
            and len(rows[0]) == 1
            and columns == [probe_result.target.column]
        )
        exact_value = rows[0][0] if has_exact_row else None
        materialize_predicate = (
            isinstance(match, PhysicalColumnBinding)
            and probe_result.probe_kind is ResearchActionKind.SEARCH_VALUE
            and source_item.required
            and source_item.kind is SemanticItemKind.FILTER
            and source_item.operator is None
            and probe_result.row_count == 1
            and has_exact_row
        )
        predicate_authority = getattr(request, "predicate_authority", None)
        if predicate_authority is not None:
            if (
                not isinstance(match, PhysicalColumnBinding)
                or source_item.kind is not SemanticItemKind.DIMENSION
                or predicate_authority.left != match.physical_column
                or predicate_authority.operator is not PredicateOperator.EQ
                or predicate_authority.right != exact_value
                or probe_result.probe_kind is not ResearchActionKind.SEARCH_VALUE
                or probe_result.row_count != 1
                or not has_exact_row
            ):
                raise RuntimeError("predicate authority has no exact value evidence")
            preserved = match.model_copy(update={"evidence_ids": evidence_ids})
            predicate_binding = DiscriminatorValueBinding(
                binding_id=(
                    f"{match.binding_id}-predicate-"
                    f"{canonical_digest(predicate_authority)[7:23]}"
                ),
                source_id=match.source_id,
                tables=match.tables,
                columns=match.columns,
                predicates=(predicate_authority,),
                join_path=match.join_path,
                evidence_ids=(probe_result.invocation_id,),
                confidence=match.confidence,
                status=BindingStatus.CANDIDATE,
                validator_rule=None,
                discriminator_column=match.physical_column,
                discriminator_predicate=predicate_authority,
            )
            updated = tuple(
                preserved if binding.binding_id == match.binding_id else binding
                for binding in updated
            )
            if all(binding.binding_id != match.binding_id for binding in updated):
                updated = (*updated, preserved)
            return (*updated, predicate_binding)
        if materialize_predicate:
            operator = (
                PredicateOperator.IS_NULL
                if exact_value is None
                else PredicateOperator.EQ
            )
            predicate = PredicateRef(
                left=match.physical_column,
                operator=operator,
                right=None if exact_value is None else exact_value,
            )
            replacement = DiscriminatorValueBinding(
                binding_id=match.binding_id,
                source_id=match.source_id,
                tables=match.tables,
                columns=match.columns,
                predicates=(predicate,),
                join_path=match.join_path,
                evidence_ids=evidence_ids,
                confidence=match.confidence,
                status=match.status,
                validator_rule="semantic-certificate:v1:discriminator_value",
                discriminator_column=match.physical_column,
                discriminator_predicate=predicate,
            )
        else:
            replacement = match.model_copy(update={"evidence_ids": evidence_ids})
        updated = tuple(
            replacement
            if binding.binding_id == match.binding_id
            else binding
            for binding in updated
        )
        if all(binding.binding_id != match.binding_id for binding in updated):
            updated = (*updated, replacement)
    return updated


logger = logging.getLogger(__name__)


# Mirrors the two targeted-research call_id prefixes minted by
# research_loop._model_call_id / _research_stop_review_call_id
# (custom_tools/text_to_sql/adaptive/research_loop.py); that module's own
# `_MODEL_CALL_ID` regex is private, so this is a literal copy rather than an
# import. Ledger records outside these prefixes (e.g. the adaptive solver's
# own "solver-generate-*" model calls) are not targeted-research reservations
# and must not be mistaken for the one this function settles.
_TARGETED_RESEARCH_MODEL_CALL_PREFIXES = ("research-model-", "research-stop-review-")


async def _unknown_usage_replay(_reservation: object) -> ModelTokenUsage:
    """Shared ``execute`` callback for both settle functions below.

    ``record_model_reservation``, ``claim_model_execution`` and
    ``record_model_started`` (``workflow/adaptive_budget_ledger.py``) are
    three separate durable sqlite transactions, so a killed process can leave
    a *durably reserved* model call in either of two states: RESERVED with no
    STARTED at all, or STARTED with no RESULT. Both settle functions replay
    the exact stored reservation through ``execute_model_call_with_budget_async``
    with a ``claim_now_ns`` pushed past ``EXECUTION_CLAIM_LEASE_NS`` so the
    stale claim is reclaimed immediately instead of waiting out the real 4h
    lease, and this callback is what that replay executes:

    - STARTED-without-RESULT: ``_claim_model_execution_step``
      (``_policy_model_budget.py``) finds the existing ``started`` record and
      settles it internally via ``_settle_model_result`` *before* returning
      control to ``execute_model_call_with_budget_async`` -- this callback is
      never awaited in that case.
    - RESERVED-without-STARTED: there is no existing ``started`` record for
      ``_claim_model_execution_step`` to settle internally, so it mints a
      brand-new one under the resumed incarnation's claim and returns
      ``_ModelExecutionReady``; ``execute_model_call_with_budget_async`` then
      awaits this callback to obtain a usage to settle *that* record with.

    Either way the model provider must not actually run for a call this is
    only replaying to unblock the ledger, so this always reports "unknown"
    usage rather than a real response. ``model_charge``
    (``custom_tools/text_to_sql/adaptive/model_budget.py``) treats
    ``ModelTokenUsage(None, None)`` as consuming the reservation's maxima --
    the same conservative charge ``_settle_model_failure`` already uses for a
    real execution failure.
    """

    return ModelTokenUsage(input_tokens=None, output_tokens=None)


async def settle_incomplete_reentry_model_call(
    runtime: object,
    solver_state: object,
    research_state: ResearchState,
    requirements: object,
) -> ResearchState:
    """Conservatively close only the exact outstanding targeted model call.

    ``record_model_reservation``, ``claim_model_execution`` and
    ``record_model_started`` (``workflow/adaptive_budget_ledger.py``) are three
    separate durable sqlite transactions, so a process kill can leave an
    outstanding reservation in either of two crash windows: RESERVED with no
    STARTED at all, or STARTED with no RESULT. Both are handled the same way
    below -- see the ``_unknown_usage_replay`` callback's docstring for why
    one execute() call safely covers both.
    """

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
    outstanding = tuple(
        record
        for record in records
        if record.reconciliation is None
        and record.reservation.call_id.startswith(_TARGETED_RESEARCH_MODEL_CALL_PREFIXES)
    )
    if not outstanding:
        return _state_with_reconciled_model_budget(research_state, ledger, policy)
    if len(outstanding) != 1:
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
        research_state,
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
        if (
            actual[0] == expected[0]
            and actual[1] == expected[1]
            and actual[2] != expected[2]
        ):
            # Same call_id and same request content, but the schema-research
            # step's model identity on record differs from what the current
            # schema_research agent profile / llm_models.yaml resolves to now.
            # This is the "model policy changed between incarnations" case
            # (see reserve_model_call_budget in _policy_model_budget.py): the
            # outstanding STARTED reservation was made under the old model, so
            # it can never be conservatively settled under the new one.
            # Falling through to `return research_state` unchanged is still
            # correct (this function must stay conservative), but that used
            # to be silent — log it so an operator resuming a run after
            # editing model config gets a diagnosable trail instead of an
            # unexplained stall.
            logger.warning(
                "model policy changed between incarnations: reentry model "
                "call_id=%s cannot be settled: model_identity on record %s "
                "!= currently resolved %s; resume is impossible for this "
                "outstanding call, start a new run",
                actual[0],
                actual[2],
                expected[2],
            )
        else:
            logger.warning(
                "outstanding reentry model call_id=%s does not match the "
                "expected replay (call_id/request/model/limits); leaving it "
                "unsettled",
                actual[0],
            )
        return research_state

    await execute_model_call_with_budget_async(
        research_state.run_id,
        research_state.run_incarnation,
        *expected,
        _unknown_usage_replay,
        config=policy,
        ledger=ledger,
        claim_now_ns=lambda: time.time_ns() + EXECUTION_CLAIM_LEASE_NS + 1,
    )
    return _state_with_reconciled_model_budget(research_state, ledger, policy)


# The adaptive solver's own model calls (workflow/text_to_sql_adaptive_solver.py
# `propose()`) mint call_ids as f"solver-generate-{revision}-{attempt}". That
# format is private to this module's caller, so this is a literal copy of the
# prefix, not an import -- same reasoning as
# _TARGETED_RESEARCH_MODEL_CALL_PREFIXES above.
_SOLVER_GENERATE_MODEL_CALL_PREFIX = "solver-generate-"


async def settle_incomplete_solver_model_call(runtime: object) -> None:
    """Force-close a stale outstanding ``solver-generate-*`` call.

    Symmetric to ``settle_incomplete_reentry_model_call`` above, but for the
    solver's own model calls instead of the research sub-loop's. A process can
    be killed in either of two durable-write windows for one
    "solver-generate-{revision}-{attempt}" ledger record: RESERVED with no
    STARTED at all, or STARTED with no RESULT (``record_model_reservation``,
    ``claim_model_execution`` and ``record_model_started`` in
    ``workflow/adaptive_budget_ledger.py`` are three separate sqlite
    transactions). Either way, that record is the *sole* outstanding
    reservation the shared per-run model-budget ledger will ever allow at a
    time (``_model_budget_chain`` in ``_policy_model_budget.py`` enforces
    "only one model reservation may be outstanding"). Every later reservation
    attempt -- including the very next solver-generate-* attempt on resume --
    is then durably blocked by ``BudgetConflictError`` until this exact record
    is settled; ``_next_solver_model_attempt``'s plain count of ledger records
    for this revision never advances past it on its own.

    A plain retry that reused this same call_id would still have to wait out
    the real ``EXECUTION_CLAIM_LEASE_NS`` (4h) lease inside
    ``claim_model_execution`` (``_adaptive_budget_ledger_common.py``) before a
    new owner is allowed to reclaim it. Passing a ``claim_now_ns`` pushed past
    the lease -- exactly as ``settle_incomplete_reentry_model_call`` does for
    the research prefixes -- makes the reclaim immediate: the outstanding
    record is settled with unknown (maximum-charged) usage instead of a real
    model response (see ``_unknown_usage_replay``'s docstring for why one
    callback safely covers both crash windows).
    """

    ledger = getattr(runtime, "budget_ledger", None)
    policy = getattr(runtime, "verified_research_policy", None)
    if ledger is None or policy is None:
        return
    records = ledger.load_model_records(runtime.run_id, runtime.run_incarnation)
    outstanding = tuple(
        record
        for record in records
        if record.reconciliation is None
        and record.reservation.call_id.startswith(
            _SOLVER_GENERATE_MODEL_CALL_PREFIX
        )
    )
    if len(outstanding) != 1:
        return

    reservation = outstanding[0].reservation
    await execute_model_call_with_budget_async(
        reservation.run_id,
        reservation.run_incarnation,
        reservation.call_id,
        reservation.request_digest,
        reservation.model_identity,
        reservation.maximum_input_tokens,
        reservation.maximum_output_tokens,
        _unknown_usage_replay,
        config=policy,
        ledger=ledger,
        claim_now_ns=lambda: time.time_ns() + EXECUTION_CLAIM_LEASE_NS + 1,
    )


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
    "settle_incomplete_solver_model_call",
)
