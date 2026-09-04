"""Pure trusted resolution tests for W3 research decisions."""

from __future__ import annotations

from pathlib import Path

import pytest

from custom_tools.text_to_sql.adaptive import decision_resolver as _resolver_module
from custom_tools.text_to_sql.adaptive.decision_resolver import (
    DecisionResolverError,
    UnresolvableModelDecisionError,
    execute_resolved_research_decision,
    resolve_research_decision,
)
from custom_tools.text_to_sql.adaptive.evidence import probe_result_to_evidence
from custom_tools.text_to_sql.adaptive.models import (
    BindingStatus,
    ColumnRef,
    DiscriminatorValueBinding,
    EvidenceCost,
    JoinType,
    PredicateOperator,
    ResearchAction,
    ResearchActionKind,
    TableRef,
)
from custom_tools.text_to_sql.adaptive.policy import canonical_action_digest
from custom_tools.text_to_sql.adaptive.probes import ProbeStatus, build_probe_result
from custom_tools.text_to_sql.adaptive.research_decision import ResearchDecisionV1
from custom_tools.text_to_sql.adaptive.semantic_reducer import (
    SemanticReducerError,
    _stable_id,
    commit_semantic_turn,
)
from custom_tools.text_to_sql.adaptive.serialization import canonical_json_bytes
from tests.text_to_sql_decision_resolver_helpers import (
    TOOL_ARGUMENTS,
    freshness as _freshness,
    make_registry as _registry,
    make_state as _state,
    resolve as _resolve,
    schema as _schema,
    stop_decision as _stop_decision,
    tool_decision as _tool_decision,
)


@pytest.mark.parametrize(("tool_name", "arguments"), TOOL_ARGUMENTS.items())
def test_resolves_all_ten_typed_intents_without_dispatch(
    tool_name: str, arguments: dict[str, object]
) -> None:
    loaded, namespace = _schema()
    registry = _registry(namespace)

    resolved = _resolve(
        _tool_decision(tool_name, arguments),
        loaded=loaded,
        namespace=namespace,
        registry=registry,
    )

    assert resolved.invocation is not None
    assert resolved.tool_claim is not None
    assert resolved.tool_claim.tool_name == tool_name
    assert resolved.invocation.tool_call.tool_name == tool_name
    assert resolved.admission.action is not None
    assert resolved.admission.action.target == resolved.tool_claim.target
    assert registry.resolve_calls == 0


def test_qualified_target_is_canonical_in_claim_and_invocation() -> None:
    resolved = _resolve(_tool_decision("inspect_table", {"table": "public.orders"}))

    assert resolved.tool_claim is not None
    assert resolved.tool_claim.target == TableRef(
        namespace="main", schema="public", table="orders"
    )
    assert resolved.invocation is not None
    assert resolved.invocation.tool_call.arguments == {"table": "public.orders"}


@pytest.mark.parametrize("logical_table", ["orders", "Orders", "missing"])
def test_missing_ambiguous_and_case_changed_tables_fail_closed(
    logical_table: str,
) -> None:
    loaded, namespace = _schema(
        {
            "public.orders": {"columns": {"id": {"type": "INTEGER"}}},
            "audit.orders": {"columns": {"id": {"type": "INTEGER"}}},
        }
    )

    with pytest.raises(UnresolvableModelDecisionError):
        _resolve(
            _tool_decision("inspect_table", {"table": logical_table}),
            loaded=loaded,
            namespace=namespace,
        )


def test_unknown_and_case_changed_columns_fail_closed() -> None:
    for column in ("Status", "missing"):
        with pytest.raises(UnresolvableModelDecisionError):
            _resolve(
                _tool_decision(
                    "inspect_column",
                    {"table": "public.orders", "column": column},
                )
            )


def test_case_only_column_mismatch_has_unique_exact_column_correction() -> None:
    loaded, namespace = _schema(
        {"public.orders": {"columns": {"OpenDate": {"type": "INTEGER"}}}}
    )

    with pytest.raises(UnresolvableModelDecisionError) as caught:
        _resolve(
            _tool_decision(
                "inspect_column",
                {"table": "public.orders", "column": "opendate"},
            ),
            loaded=loaded,
            namespace=namespace,
        )

    assert caught.value.exact_column == ColumnRef(
        table=TableRef(namespace="main", schema="public", table="orders"),
        column="OpenDate",
    )


@pytest.mark.parametrize(
    ("tables", "column"),
    (
        ({"public.orders": {"columns": {"OpenDate": {"type": "INTEGER"}}}}, "missing"),
        (
            {
                "public.orders": {
                    "columns": {
                        "OpenDate": {"type": "INTEGER"},
                        "OPENDATE": {"type": "INTEGER"},
                    }
                }
            },
            "opendate",
        ),
    ),
    ids=("unknown", "ambiguous-case-folded"),
)
def test_unknown_or_ambiguous_column_has_no_exact_column_correction(
    tables: dict[str, object],
    column: str,
) -> None:
    loaded, namespace = _schema(tables)

    with pytest.raises(UnresolvableModelDecisionError) as caught:
        _resolve(
            _tool_decision(
                "inspect_column",
                {"table": "public.orders", "column": column},
            ),
            loaded=loaded,
            namespace=namespace,
        )

    assert caught.value.exact_column is None


def test_missing_schema_document_is_a_model_reference_error() -> None:
    with pytest.raises(UnresolvableModelDecisionError):
        _resolve(
            _tool_decision(
                "read_schema_evidence",
                {"document_id": "missing-document"},
            )
        )


def test_cross_runtime_namespace_and_schema_version_fail_closed() -> None:
    loaded, namespace = _schema()
    state = _state(namespace)
    registry = _registry(namespace)
    registry.context.data_runtime.table_namespace = "other"
    with pytest.raises(DecisionResolverError) as runtime_error:
        _resolve(
            _tool_decision("inspect_table", {"table": "public.orders"}),
            loaded=loaded,
            namespace=namespace,
            state=state,
            registry=registry,
        )
    assert type(runtime_error.value) is DecisionResolverError

    other_loaded, other_namespace = _schema(
        {"public.other": {"columns": {"id": {"type": "INTEGER"}}}}
    )
    with pytest.raises(DecisionResolverError):
        _resolve(
            _tool_decision("inspect_table", {"table": "public.orders"}),
            loaded=other_loaded,
            namespace=other_namespace,
            state=state,
            registry=_registry(other_namespace),
        )


@pytest.mark.parametrize("reason", ["complete", "ambiguous", "unsupported"])
def test_resolves_three_stop_reasons_without_tool_dispatch(reason: str) -> None:
    loaded, namespace = _schema()
    state = _state(namespace, with_evidence=True, required=False)
    registry = _registry(namespace)

    resolved = resolve_research_decision(
        state,
        _stop_decision(reason),
        loaded_schema=loaded,
        freshness_context=_freshness(state),
        registry=registry,
    )

    assert resolved.is_stop
    assert resolved.tool_claim is None
    assert resolved.admission.action is None
    assert execute_resolved_research_decision(resolved, registry) is None
    assert registry.resolve_calls == 0


def test_resolves_semantic_commit_without_tool_claim_or_invocation() -> None:
    loaded, namespace = _schema()
    state = _state(namespace, with_evidence=True, required=False)
    registry = _registry(namespace)
    decision = ResearchDecisionV1.model_validate(
        {
            "proposals": (
                {
                    "proposal_type": "new_hypothesis",
                    "proposal_key": "proposal:orders",
                    "source_ids": ("source-1",),
                    "claim": "orders are relevant",
                    "candidate_targets": (
                        {"target_kind": "table", "table": "public.orders"},
                    ),
                    "citation_evidence_ids": ("evidence-1",),
                },
            ),
            "next": {"next_kind": "semantic_commit"},
        }
    )

    resolved = resolve_research_decision(
        state,
        decision,
        loaded_schema=loaded,
        freshness_context=_freshness(state),
        registry=registry,
    )

    assert resolved.tool_claim is None
    assert resolved.invocation is None
    assert resolved.admission.action is not None
    assert resolved.admission.action.target is None
    assert registry.resolve_calls == 0


def test_complete_stop_cannot_hide_unresolved_required_item() -> None:
    loaded, namespace = _schema()
    state = _state(namespace, with_evidence=True, required=True)
    with pytest.raises(DecisionResolverError, match="unresolved required"):
        resolve_research_decision(
            state,
            _stop_decision("complete"),
            loaded_schema=loaded,
            freshness_context=_freshness(state),
            registry=_registry(namespace),
        )


@pytest.mark.parametrize(
    "mutation",
    ["missing", "foreign_run", "foreign_incarnation", "stale_schema", "future"],
)
def test_missing_foreign_stale_and_future_citations_fail_closed(
    mutation: str,
) -> None:
    loaded, namespace = _schema()
    state = _state(namespace, with_evidence=True, required=False)
    citation = "missing" if mutation == "missing" else "evidence-1"
    if mutation != "missing":
        record = state.evidence[0]
        changes = {
            "foreign_run": {"run_id": "run-2"},
            "foreign_incarnation": {"run_incarnation": "incarnation-2"},
            "stale_schema": {"schema_namespace_version": "sha256:" + "f" * 64},
            "future": {"revision": state.revision + 1},
        }[mutation]
        state = state.model_copy(
            update={"evidence": (record.model_copy(update=changes),)}
        )
    decision = ResearchDecisionV1.model_validate(
        {
            "proposals": (),
            "next": {
                "next_kind": "stop",
                "reason": "unsupported",
                "source_ids": ("source-1",),
                "citation_evidence_ids": (citation,),
            },
        },
        strict=True,
    )
    with pytest.raises(DecisionResolverError):
        resolve_research_decision(
            state,
            decision,
            loaded_schema=loaded,
            freshness_context=_freshness(state),
            registry=_registry(namespace),
        )


@pytest.mark.parametrize(
    "proposal",
    [
        {
            "proposal_type": "hypothesis_assessment",
            "subject": {"reference_kind": "existing", "hypothesis_id": "missing"},
            "certificate": "insufficient",
            "citation_evidence_ids": ("evidence-1",),
        },
        {
            "proposal_type": "binding_assessment",
            "subject": {"reference_kind": "existing", "binding_id": "missing"},
            "certificate": "insufficient",
            "citation_evidence_ids": ("evidence-1",),
        },
        {
            "proposal_type": "join_assessment",
            "subject": {"reference_kind": "existing", "join_id": "missing"},
            "certificate": "insufficient",
            "citation_evidence_ids": ("evidence-1",),
        },
    ],
)
def test_unknown_existing_assessment_references_fail_closed(
    proposal: dict[str, object],
) -> None:
    loaded, namespace = _schema()
    state = _state(namespace, with_evidence=True, required=False)
    decision = ResearchDecisionV1.model_validate(
        {
            "proposals": (proposal,),
            "next": {
                "next_kind": "tool",
                "hypothesis_ref": None,
                "intent": {
                    "tool_name": "inspect_column",
                    "arguments": {"table": "public.orders", "column": "status"},
                },
            },
        },
        strict=True,
    )
    with pytest.raises(UnresolvableModelDecisionError):
        resolve_research_decision(
            state,
            decision,
            loaded_schema=loaded,
            freshness_context=_freshness(state),
            registry=_registry(namespace),
        )


def test_model_semantic_admission_error_is_retryable() -> None:
    loaded, namespace = _schema()
    state = _state(namespace, with_evidence=True, required=False)
    proposals = tuple(
        {
            "proposal_type": "new_binding",
            "proposal_key": f"proposal:binding-{index}",
            "source_id": "source-1",
            "candidate": {
                "kind": "discriminator_value",
                "discriminator_column": {"table": table, "column": "status"},
                "discriminator_predicate": {
                    "left": {"table": table, "column": "status"},
                    "operator": PredicateOperator.EQ,
                    "right": "paid",
                },
            },
            "join_references": (),
            "citation_evidence_ids": ("evidence-1",),
        }
        for index, table in enumerate(("public.orders", "orders"), start=1)
    )
    decision = ResearchDecisionV1.model_validate(
        {
            "proposals": proposals,
            "next": {
                "next_kind": "tool",
                "hypothesis_ref": None,
                "intent": {
                    "tool_name": "inspect_column",
                    "arguments": {
                        "table": "public.orders",
                        "column": "status",
                    },
                },
            },
        },
        strict=True,
    )

    with pytest.raises(
        UnresolvableModelDecisionError,
        match="model semantic decision is not admissible",
    ):
        resolve_research_decision(
            state,
            decision,
            loaded_schema=loaded,
            freshness_context=_freshness(state),
            registry=_registry(namespace),
        )


def test_new_binding_precheck_preserves_retryable_semantic_errors() -> None:
    loaded, namespace = _schema()
    state = _state(namespace, with_evidence=True, required=False)
    decision = ResearchDecisionV1.model_validate(
        {
            "proposals": (
                {
                    "proposal_type": "new_binding",
                    "proposal_key": "proposal:paid-status",
                    "source_id": "source-1",
                    "candidate": {
                        "kind": "discriminator_value",
                        "discriminator_column": {
                            "table": "public.orders",
                            "column": "status",
                        },
                        "discriminator_predicate": {
                            "left": {"table": "public.orders", "column": "id"},
                            "operator": PredicateOperator.EQ,
                            "right": "paid",
                        },
                    },
                    "join_references": (),
                    "citation_evidence_ids": ("evidence-1",),
                },
            ),
            "next": {
                "next_kind": "tool",
                "hypothesis_ref": None,
                "intent": {
                    "tool_name": "inspect_column",
                    "arguments": {
                        "table": "public.orders",
                        "column": "status",
                    },
                },
            },
        },
        strict=True,
    )

    with pytest.raises(
        UnresolvableModelDecisionError,
        match="model semantic decision is not admissible",
    ):
        resolve_research_decision(
            state,
            decision,
            loaded_schema=loaded,
            freshness_context=_freshness(state),
            registry=_registry(namespace),
        )


def _semantic_commit(proposals: tuple[dict[str, object], ...]) -> ResearchDecisionV1:
    return ResearchDecisionV1.model_validate(
        {"proposals": proposals, "next": {"next_kind": "semantic_commit"}},
        strict=True,
    )


def _commit_candidate(
    state,
    decision: ResearchDecisionV1,
    *,
    loaded,
    namespace,
):
    resolved = resolve_research_decision(
        state,
        decision,
        loaded_schema=loaded,
        freshness_context=_freshness(state),
        registry=_registry(namespace),
    )
    return commit_semantic_turn(resolved.admission).state


def test_single_predicate_discriminator_keeps_legacy_binding_id() -> None:
    loaded, namespace = _schema()
    state = _commit_candidate(
        _state(namespace, with_evidence=True, required=False),
        _semantic_commit(
            (
                {
                    "proposal_type": "new_binding",
                    "proposal_key": "proposal:paid-status",
                    "source_id": "source-1",
                    "candidate": {
                        "kind": "discriminator_value",
                        "discriminator_column": {
                            "table": "public.orders",
                            "column": "status",
                        },
                        "discriminator_predicate": {
                            "left": {
                                "table": "public.orders",
                                "column": "status",
                            },
                            "operator": PredicateOperator.EQ,
                            "right": "paid",
                        },
                    },
                    "join_references": (),
                    "citation_evidence_ids": ("evidence-1",),
                },
            )
        ),
        loaded=loaded,
        namespace=namespace,
    )
    binding = state.bindings[0]

    assert binding.binding_id == _stable_id(
        "binding",
        {
            "schema": state.schema_namespace_version,
            "kind": "discriminator_value",
            "source": "source-1",
            "column": binding.discriminator_column.model_dump(mode="json"),
            "predicate": binding.discriminator_predicate.model_dump(mode="json"),
        },
    )


def test_loaded_schema_supports_physical_column_without_inspect_column() -> None:
    loaded, namespace = _schema()
    initial = _state(namespace, with_evidence=True, required=False)
    candidate_state = _commit_candidate(
        initial,
        _semantic_commit(
            (
                {
                    "proposal_type": "new_binding",
                    "proposal_key": "proposal:physical-status",
                    "source_id": "source-1",
                    "candidate": {
                        "kind": "physical_column",
                        "physical_column": {
                            "table": "public.orders",
                            "column": "status",
                        },
                    },
                    "join_references": (),
                    "citation_evidence_ids": ("evidence-1",),
                },
            )
        ),
        loaded=loaded,
        namespace=namespace,
    )
    binding = candidate_state.bindings[0]
    assessment = _semantic_commit(
        (
            {
                "proposal_type": "binding_assessment",
                "subject": {
                    "reference_kind": "existing",
                    "binding_id": binding.binding_id,
                },
                "certificate": "consistent",
                "citation_evidence_ids": ("evidence-1",),
            },
        )
    )

    resolved = resolve_research_decision(
        candidate_state,
        assessment,
        loaded_schema=loaded,
        freshness_context=_freshness(candidate_state),
        registry=_registry(namespace),
    )

    assert resolved.invocation is None
    assert resolved.admission.bindings[0].status is BindingStatus.SUPPORTED


def test_physical_column_with_join_route_is_a_distinct_binding() -> None:
    loaded, namespace = _schema(
        {
            "public.orders": {
                "columns": {
                    "customer_id": {"type": "INTEGER"},
                    "status": {"type": "TEXT"},
                }
            },
            "public.customers": {"columns": {"id": {"type": "INTEGER"}}},
        }
    )
    initial = _state(namespace, with_evidence=True, required=False)
    candidate_state = _commit_candidate(
        initial,
        _semantic_commit(
            (
                {
                    "proposal_type": "new_binding",
                    "proposal_key": "proposal:physical-status",
                    "source_id": "source-1",
                    "candidate": {
                        "kind": "physical_column",
                        "physical_column": {
                            "table": "public.orders",
                            "column": "status",
                        },
                    },
                    "join_references": (),
                    "citation_evidence_ids": ("evidence-1",),
                },
                {
                    "proposal_type": "new_join",
                    "proposal_key": "proposal:orders-customer",
                    "left": {
                        "table": "public.orders",
                        "column": "customer_id",
                    },
                    "right": {"table": "public.customers", "column": "id"},
                    "join_type": JoinType.INNER,
                    "path": (),
                    "citation_evidence_ids": ("evidence-1",),
                },
            )
        ),
        loaded=loaded,
        namespace=namespace,
    )
    binding = candidate_state.bindings[0]
    supported_state = _commit_candidate(
        candidate_state,
        _semantic_commit(
            (
                {
                    "proposal_type": "binding_assessment",
                    "subject": {
                        "reference_kind": "existing",
                        "binding_id": binding.binding_id,
                    },
                    "certificate": "consistent",
                    "citation_evidence_ids": ("evidence-1",),
                },
            )
        ),
        loaded=loaded,
        namespace=namespace,
    )
    join = supported_state.join_candidates[0]

    routed_state = _commit_candidate(
        supported_state,
        _semantic_commit(
            (
                {
                    "proposal_type": "new_binding",
                    "proposal_key": "proposal:routed-physical-status",
                    "source_id": "source-1",
                    "candidate": {
                        "kind": "physical_column",
                        "physical_column": {
                            "table": "public.orders",
                            "column": "status",
                        },
                    },
                    "join_references": (
                        {"reference_kind": "existing", "join_id": join.join_id},
                    ),
                    "citation_evidence_ids": ("evidence-1",),
                },
            )
        ),
        loaded=loaded,
        namespace=namespace,
    )

    assert len(routed_state.bindings) == 2
    assert routed_state.bindings[1].binding_id != binding.binding_id
    assert routed_state.bindings[1].join_path == join.path


@pytest.mark.parametrize(
    "candidate",
    (
        {
            "kind": "discriminator_value",
            "discriminator_column": {
                "table": "public.orders",
                "column": "status",
            },
            "discriminator_predicate": {
                "left": {"table": "public.orders", "column": "status"},
                "operator": PredicateOperator.EQ,
                "right": "open",
            },
        },
        {
            "kind": "derived_expression",
            "expression_claim": "open orders",
            "document_id": "schema-doc",
            "rule_excerpt": "status is open",
            "input_columns": (
                {"table": "public.orders", "column": "status"},
            ),
        },
    ),
)
def test_semantic_binding_with_join_route_is_a_distinct_binding(
    candidate: dict[str, object],
) -> None:
    loaded, namespace = _schema(
        {
            "public.orders": {
                "columns": {
                    "customer_id": {"type": "INTEGER"},
                    "status": {"type": "TEXT"},
                }
            },
            "public.customers": {"columns": {"id": {"type": "INTEGER"}}},
        }
    )
    initial = _state(namespace, with_evidence=True, required=False)
    candidate_state = _commit_candidate(
        initial,
        _semantic_commit(
            (
                {
                    "proposal_type": "new_binding",
                    "proposal_key": "proposal:binding",
                    "source_id": "source-1",
                    "candidate": candidate,
                    "join_references": (),
                    "citation_evidence_ids": ("evidence-1",),
                },
                {
                    "proposal_type": "new_join",
                    "proposal_key": "proposal:orders-customer",
                    "left": {
                        "table": "public.orders",
                        "column": "customer_id",
                    },
                    "right": {"table": "public.customers", "column": "id"},
                    "join_type": JoinType.INNER,
                    "path": (),
                    "citation_evidence_ids": ("evidence-1",),
                },
            )
        ),
        loaded=loaded,
        namespace=namespace,
    )
    binding = candidate_state.bindings[0]
    join = candidate_state.join_candidates[0]

    routed_state = _commit_candidate(
        candidate_state,
        _semantic_commit(
            (
                {
                    "proposal_type": "new_binding",
                    "proposal_key": "proposal:routed-binding",
                    "source_id": "source-1",
                    "candidate": candidate,
                    "join_references": (
                        {"reference_kind": "existing", "join_id": join.join_id},
                    ),
                    "citation_evidence_ids": ("evidence-1",),
                },
            )
        ),
        loaded=loaded,
        namespace=namespace,
    )

    assert len(routed_state.bindings) == 2
    assert routed_state.bindings[1].binding_id != binding.binding_id
    assert routed_state.bindings[1].join_path == join.path


def test_loaded_schema_validates_declared_fk_without_inspect_relationships() -> None:
    loaded, namespace = _schema(
        {
            "public.orders": {
                "columns": {
                    "id": {"type": "INTEGER"},
                    "customer_id": {"type": "INTEGER"},
                },
                "foreign_keys": {
                    "complete": True,
                    "constraints": [
                        {
                            "constraint_id": "orders_customer",
                            "to_table": "public.customers",
                            "column_pairs": [
                                {
                                    "from_column": "customer_id",
                                    "to_column": "id",
                                }
                            ],
                        }
                    ],
                },
            },
            "public.customers": {"columns": {"id": {"type": "INTEGER"}}},
        }
    )
    initial = _state(namespace, with_evidence=True, required=False)
    candidate_state = _commit_candidate(
        initial,
        _semantic_commit(
            (
                {
                    "proposal_type": "new_join",
                    "proposal_key": "proposal:orders-customer",
                    "left": {"table": "public.orders", "column": "customer_id"},
                    "right": {"table": "public.customers", "column": "id"},
                    "join_type": JoinType.INNER,
                    "path": (),
                    "citation_evidence_ids": ("evidence-1",),
                },
            )
        ),
        loaded=loaded,
        namespace=namespace,
    )
    join = candidate_state.join_candidates[0]
    assert join.status.value == "validated"
    assessment = _semantic_commit(
        (
            {
                "proposal_type": "join_assessment",
                "subject": {
                    "reference_kind": "existing",
                    "join_id": join.join_id,
                },
                "certificate": "consistent",
                "citation_evidence_ids": ("evidence-1",),
            },
        )
    )

    resolved = resolve_research_decision(
        candidate_state,
        assessment,
        loaded_schema=loaded,
        freshness_context=_freshness(candidate_state),
        registry=_registry(namespace),
    )

    assert resolved.invocation is None
    assert resolved.admission.join_candidates[0].status.value == "validated"


def test_loaded_schema_columns_validate_multihop_join_without_declared_fk() -> None:
    loaded, namespace = _schema(
        {
            "main.atom": {"columns": {"atom_id": {"type": "TEXT"}}},
            "main.connected": {
                "columns": {
                    "atom_id": {"type": "TEXT"},
                    "bond_id": {"type": "TEXT"},
                }
            },
            "main.bond": {"columns": {"bond_id": {"type": "TEXT"}}},
        }
    )
    initial = _state(namespace, with_evidence=True, required=False)
    candidate_state = _commit_candidate(
        initial,
        _semantic_commit(
            (
                {
                    "proposal_type": "new_join",
                    "proposal_key": "proposal:atom-bond",
                    "left": {"table": "main.atom", "column": "atom_id"},
                    "right": {"table": "main.bond", "column": "bond_id"},
                    "join_type": JoinType.INNER,
                    "path": (
                        {
                            "left": {"table": "main.atom", "column": "atom_id"},
                            "right": {
                                "table": "main.connected",
                                "column": "atom_id",
                            },
                            "join_type": JoinType.INNER,
                        },
                        {
                            "left": {
                                "table": "main.connected",
                                "column": "bond_id",
                            },
                            "right": {"table": "main.bond", "column": "bond_id"},
                            "join_type": JoinType.INNER,
                        },
                    ),
                    "citation_evidence_ids": ("evidence-1",),
                },
            )
        ),
        loaded=loaded,
        namespace=namespace,
    )
    join = candidate_state.join_candidates[0]
    assert join.status.value == "candidate"

    resolved = resolve_research_decision(
        candidate_state,
        _semantic_commit(
            (
                {
                    "proposal_type": "join_assessment",
                    "subject": {
                        "reference_kind": "existing",
                        "join_id": join.join_id,
                    },
                    "certificate": "consistent",
                    "citation_evidence_ids": ("evidence-1",),
                },
            )
        ),
        loaded_schema=loaded,
        freshness_context=_freshness(candidate_state),
        registry=_registry(namespace),
    )

    assert resolved.admission.join_candidates[0].status.value == "validated"


def test_loaded_schema_accepts_eq_in_join_path() -> None:
    loaded, namespace = _schema(
        {
            "public.orders": {
                "columns": {"customer_id": {"type": "INTEGER"}},
            },
            "public.customers": {"columns": {"id": {"type": "INTEGER"}}},
        }
    )
    state = _state(namespace, with_evidence=True, required=False)

    resolved = resolve_research_decision(
        state,
        _semantic_commit(
            (
                {
                    "proposal_type": "new_join",
                    "proposal_key": "proposal:orders-customer",
                    "left": {"table": "public.orders", "column": "customer_id"},
                    "right": {"table": "public.customers", "column": "id"},
                    "join_type": JoinType.INNER,
                    "path": (
                        {
                            "left": {
                                "table": "public.orders",
                                "column": "customer_id",
                            },
                            "right": {
                                "table": "public.customers",
                                "column": "id",
                            },
                            "operator": PredicateOperator.EQ,
                            "join_type": JoinType.INNER,
                        },
                    ),
                    "citation_evidence_ids": ("evidence-1",),
                },
            )
        ),
        loaded_schema=loaded,
        freshness_context=_freshness(state),
        registry=_registry(namespace),
    )

    assert resolved.admission.join_candidates[0].status.value == "validated"


def test_loaded_schema_allows_join_between_existing_columns_without_fk() -> None:
    loaded, namespace = _schema(
        {
            "public.orders": {
                "columns": {"customer_id": {"type": "INTEGER"}},
            },
            "public.customers": {"columns": {"id": {"type": "INTEGER"}}},
        }
    )
    state = _state(namespace, with_evidence=True, required=False)

    resolved = resolve_research_decision(
        state,
        _semantic_commit(
            (
                {
                    "proposal_type": "new_join",
                    "proposal_key": "proposal:orders-customer",
                    "left": {"table": "public.orders", "column": "customer_id"},
                    "right": {"table": "public.customers", "column": "id"},
                    "join_type": JoinType.INNER,
                    "path": (),
                    "citation_evidence_ids": ("evidence-1",),
                },
            )
        ),
        loaded_schema=loaded,
        freshness_context=_freshness(state),
        registry=_registry(namespace),
    )

    assert resolved.admission.join_candidates[0].status.value == "validated"


def test_loaded_schema_validates_composite_declared_fk_without_inspect_relationships() -> None:
    loaded, namespace = _schema(
        {
            "public.orders": {
                "columns": {
                    "customer_a": {"type": "INTEGER"},
                    "customer_b": {"type": "INTEGER"},
                },
                "foreign_keys": {
                    "complete": True,
                    "constraints": [
                        {
                            "constraint_id": "orders_customer",
                            "to_table": "public.customers",
                            "column_pairs": [
                                {"from_column": "customer_a", "to_column": "id_a"},
                                {"from_column": "customer_b", "to_column": "id_b"},
                            ],
                        }
                    ],
                },
            },
            "public.customers": {
                "columns": {
                    "id_a": {"type": "INTEGER"},
                    "id_b": {"type": "INTEGER"},
                }
            },
        }
    )
    state = _state(namespace, with_evidence=True, required=False)
    resolved = resolve_research_decision(
        state,
        _semantic_commit(
            (
                {
                    "proposal_type": "new_join",
                    "proposal_key": "proposal:orders-customer",
                    "left": {"table": "public.orders", "column": "customer_a"},
                    "right": {"table": "public.customers", "column": "id_a"},
                    "join_type": JoinType.INNER,
                    "path": (
                        {
                            "left": {
                                "table": "public.orders",
                                "column": "customer_a",
                            },
                            "right": {
                                "table": "public.customers",
                                "column": "id_a",
                            },
                            "join_type": JoinType.INNER,
                        },
                        {
                            "left": {
                                "table": "public.orders",
                                "column": "customer_b",
                            },
                            "right": {
                                "table": "public.customers",
                                "column": "id_b",
                            },
                            "join_type": JoinType.INNER,
                        },
                    ),
                    "citation_evidence_ids": ("evidence-1",),
                },
            )
        ),
        loaded_schema=loaded,
        freshness_context=_freshness(state),
        registry=_registry(namespace),
    )

    assert resolved.admission.join_candidates[0].status.value == "validated"


def test_loaded_schema_does_not_validate_fk_with_mismatched_join_endpoints() -> None:
    loaded, namespace = _schema(
        {
            "public.orders": {
                "columns": {
                    "id": {"type": "INTEGER"},
                    "customer_id": {"type": "INTEGER"},
                },
                "foreign_keys": {
                    "complete": True,
                    "constraints": [
                        {
                            "constraint_id": "orders_customer",
                            "to_table": "public.customers",
                            "column_pairs": [
                                {
                                    "from_column": "customer_id",
                                    "to_column": "id",
                                }
                            ],
                        }
                    ],
                },
            },
            "public.customers": {"columns": {"id": {"type": "INTEGER"}}},
        }
    )
    decision = _semantic_commit(
        (
            {
                "proposal_type": "new_join",
                "proposal_key": "proposal:mismatched-endpoints",
                "left": {"table": "public.orders", "column": "id"},
                "right": {"table": "public.customers", "column": "id"},
                "join_type": JoinType.INNER,
                "path": (
                    {
                        "left": {
                            "table": "public.orders",
                            "column": "customer_id",
                        },
                        "right": {"table": "public.customers", "column": "id"},
                        "join_type": JoinType.INNER,
                    },
                ),
                "citation_evidence_ids": ("evidence-1",),
            },
        )
    )

    state = _state(namespace, with_evidence=True, required=False)
    resolved = resolve_research_decision(
        state,
        decision,
        loaded_schema=loaded,
        freshness_context=_freshness(state),
        registry=_registry(namespace),
    )

    assert resolved.admission.join_candidates[0].status.value == "candidate"


def test_loaded_schema_rejects_missing_physical_column() -> None:
    loaded, namespace = _schema()
    initial = _state(namespace, with_evidence=True, required=False)

    with pytest.raises(UnresolvableModelDecisionError):
        _commit_candidate(
            initial,
            _semantic_commit(
                (
                    {
                        "proposal_type": "new_binding",
                        "proposal_key": "proposal:missing-column",
                        "source_id": "source-1",
                        "candidate": {
                            "kind": "physical_column",
                            "physical_column": {
                                "table": "public.orders",
                                "column": "missing",
                            },
                        },
                        "join_references": (),
                        "citation_evidence_ids": ("evidence-1",),
                    },
                )
            ),
            loaded=loaded,
            namespace=namespace,
        )


def test_loaded_schema_certifies_valid_discriminator_literal() -> None:
    loaded, namespace = _schema(
        {
            "public.orders": {
                "columns": {
                    "id": {"type": "INTEGER"},
                    "customer_id": {"type": "INTEGER"},
                    "status": {"type": "TEXT"},
                }
            },
            "public.customers": {"columns": {"id": {"type": "INTEGER"}}},
        }
    )
    initial = _state(namespace, with_evidence=True, required=False)
    discriminator_state = _commit_candidate(
        initial,
        _semantic_commit(
            (
                {
                    "proposal_type": "new_binding",
                    "proposal_key": "proposal:paid-status",
                    "source_id": "source-1",
                    "candidate": {
                        "kind": "discriminator_value",
                        "discriminator_column": {
                            "table": "public.orders",
                            "column": "status",
                        },
                        "discriminator_predicate": {
                            "left": {"table": "public.orders", "column": "status"},
                            "operator": PredicateOperator.EQ,
                            "right": "paid",
                        },
                    },
                    "join_references": (),
                    "citation_evidence_ids": ("evidence-1",),
                },
            )
        ),
        loaded=loaded,
        namespace=namespace,
    )
    resolved = resolve_research_decision(
        discriminator_state,
        _semantic_commit(
            (
                {
                    "proposal_type": "binding_assessment",
                    "subject": {
                        "reference_kind": "existing",
                        "binding_id": discriminator_state.bindings[0].binding_id,
                    },
                    "certificate": "consistent",
                    "citation_evidence_ids": ("evidence-1",),
                },
            )
        ),
        loaded_schema=loaded,
        freshness_context=_freshness(discriminator_state),
        registry=_registry(namespace),
    )
    assert resolved.admission.bindings[0].status is BindingStatus.SUPPORTED


@pytest.mark.parametrize(
    ("requested_value", "rows", "accepted"),
    (
        (31, [["31"]], True),
        ("31", [["31"]], True),
        (31, [], True),
    ),
)
def test_search_value_result_does_not_gate_valid_discriminator_literal(
    requested_value: str | int,
    rows: list[list[str]],
    accepted: bool,
) -> None:
    loaded, namespace = _schema()
    state = _state(namespace, required=False)
    target = ColumnRef(
        table=TableRef(namespace="main", schema="public", table="orders"),
        column="status",
    )
    action = ResearchAction(
        action_id="search-value-action",
        kind=ResearchActionKind.SEARCH_VALUE,
        hypothesis_id=None,
        target=target,
        parameters=(("top_k", 1), ("value", requested_value)),
        action_digest=canonical_action_digest(
            kind=ResearchActionKind.SEARCH_VALUE,
            hypothesis_id=None,
            target=target,
            parameters=(("top_k", 1), ("value", requested_value)),
            expected_revision=state.revision,
        ),
        expected_revision=state.revision,
    )
    payload = {
        "columns": ["status"],
        "probe_kind": ResearchActionKind.SEARCH_VALUE.value,
        "schema_namespace_version": state.schema_namespace_version,
        "target": target.model_dump(mode="json", by_alias=True),
        "rows": rows,
        "requested_value": requested_value,
    }
    result = build_probe_result(
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        revision=state.revision,
        schema_namespace_version=state.schema_namespace_version,
        invocation_id="search-value-evidence",
        action_digest=action.action_digest,
        probe_kind=action.kind,
        status=ProbeStatus.SUCCESS,
        target=target,
        started_at=_freshness(state).evaluated_at,
        completed_at=_freshness(state).evaluated_at,
        summary="exact requested scalar",
        cost=EvidenceCost(
            wall_clock_ms=0,
            model_calls=0,
            model_tokens=0,
                db_probe_ms=0,
                rows=len(rows),
                bytes=len(canonical_json_bytes(payload)),
        ),
        row_count=len(rows),
        truncated=False,
        payload=payload,
    )
    evidence = probe_result_to_evidence(result, action)
    assert evidence is not None
    state = state.model_copy(
        update={
            "revision": state.revision + 1,
            "evidence": (evidence,),
            "action_history": (action,),
        }
    )
    candidate_state = _commit_candidate(
        state,
        _semantic_commit(
            (
                {
                    "proposal_type": "new_binding",
                    "proposal_key": "proposal:status-code",
                    "source_id": "source-1",
                    "candidate": {
                        "kind": "discriminator_value",
                        "discriminator_column": {
                            "table": "public.orders",
                            "column": "status",
                        },
                        "discriminator_predicate": {
                            "left": {"table": "public.orders", "column": "status"},
                            "operator": PredicateOperator.EQ,
                            "right": 31,
                        },
                    },
                    "join_references": (),
                    "citation_evidence_ids": (evidence.evidence_id,),
                },
            )
        ),
        loaded=loaded,
        namespace=namespace,
    )
    assessment = _semantic_commit(
        (
            {
                "proposal_type": "binding_assessment",
                "subject": {
                    "reference_kind": "existing",
                    "binding_id": candidate_state.bindings[0].binding_id,
                },
                "certificate": "consistent",
                "citation_evidence_ids": (evidence.evidence_id,),
            },
        )
    )

    if accepted:
        resolved = resolve_research_decision(
            candidate_state,
            assessment,
            loaded_schema=loaded,
            freshness_context=_freshness(candidate_state),
            registry=_registry(namespace),
        )
        assert resolved.admission.bindings[0].status is BindingStatus.SUPPORTED
    else:
        with pytest.raises(UnresolvableModelDecisionError):
            resolve_research_decision(
                candidate_state,
                assessment,
                loaded_schema=loaded,
                freshness_context=_freshness(candidate_state),
                registry=_registry(namespace),
            )


def test_resolver_preserves_all_physical_predicates_of_calendar_binding() -> None:
    loaded, namespace = _schema(
        {
            "public.orders": {"columns": {"id": {"type": "INTEGER"}}},
            "public.events": {
                "columns": {
                    "year": {"type": "INTEGER"},
                    "month": {"type": "INTEGER"},
                }
            },
        }
    )
    initial = _state(namespace, with_evidence=True, required=False)
    state = _commit_candidate(
        initial,
        _semantic_commit(
            (
                {
                    "proposal_type": "new_binding",
                    "proposal_key": "proposal:calendar-period",
                    "source_id": "source-1",
                    "candidate": {
                        "kind": "discriminator_value",
                        "discriminator_column": {
                            "table": "public.events",
                            "column": "year",
                        },
                        "discriminator_predicate": {
                            "left": {"table": "public.events", "column": "year"},
                            "operator": PredicateOperator.EQ,
                            "right": 2024,
                        },
                        "additional_predicates": (
                            {
                                "left": {
                                    "table": "public.events",
                                    "column": "month",
                                },
                                "operator": PredicateOperator.EQ,
                                "right": 6,
                            },
                        ),
                    },
                    "join_references": (),
                    "citation_evidence_ids": ("evidence-1",),
                },
            )
        ),
        loaded=loaded,
        namespace=namespace,
    )

    binding = state.bindings[0]
    assert isinstance(binding, DiscriminatorValueBinding)
    assert tuple(column.column for column in binding.columns) == ("year", "month")
    assert tuple(predicate.left.column for predicate in binding.predicates) == (
        "year",
        "month",
    )


def test_trusted_semantic_admission_error_remains_terminal(monkeypatch) -> None:
    loaded, namespace = _schema()
    state = _state(namespace)
    calls = 0

    def reject_trusted_state(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise SemanticReducerError("trusted invariant failed")

    monkeypatch.setattr(
        _resolver_module,
        "admit_semantic_turn",
        reject_trusted_state,
    )

    with pytest.raises(DecisionResolverError) as error:
        resolve_research_decision(
            state,
            _tool_decision(
                "inspect_column",
                {"table": "public.orders", "column": "status"},
            ),
            loaded_schema=loaded,
            freshness_context=_freshness(state),
            registry=_registry(namespace),
        )

    assert type(error.value) is DecisionResolverError
    assert calls == 2


def test_unknown_source_and_tool_hypothesis_references_fail_closed() -> None:
    loaded, namespace = _schema()
    state = _state(namespace, with_evidence=True, required=False)
    missing_source = ResearchDecisionV1.model_validate(
        {
            "proposals": (
                {
                    "proposal_type": "new_hypothesis",
                    "proposal_key": "proposal:h1",
                    "source_ids": ("missing-source",),
                    "claim": "unknown source",
                    "candidate_targets": (
                        {"target_kind": "table", "table": "public.orders"},
                    ),
                    "citation_evidence_ids": ("evidence-1",),
                },
            ),
            "next": {
                "next_kind": "tool",
                "hypothesis_ref": None,
                "intent": {
                    "tool_name": "inspect_column",
                    "arguments": {"table": "public.orders", "column": "status"},
                },
            },
        },
        strict=True,
    )
    missing_hypothesis = ResearchDecisionV1.model_validate(
        {
            "proposals": (),
            "next": {
                "next_kind": "tool",
                "hypothesis_ref": {
                    "reference_kind": "existing",
                    "hypothesis_id": "missing-hypothesis",
                },
                "intent": {
                    "tool_name": "inspect_table",
                    "arguments": {"table": "public.orders"},
                },
            },
        },
        strict=True,
    )

    for decision in (missing_source, missing_hypothesis):
        with pytest.raises(DecisionResolverError):
            resolve_research_decision(
                state,
                decision,
                loaded_schema=loaded,
                freshness_context=_freshness(state),
                registry=_registry(namespace),
            )


def test_local_hypothesis_ref_becomes_persistent_before_execution() -> None:
    loaded, namespace = _schema()
    state = _state(namespace, with_evidence=True, required=False)
    decision = ResearchDecisionV1.model_validate(
        {
            "proposals": (
                {
                    "proposal_type": "new_hypothesis",
                    "proposal_key": "proposal:h1",
                    "source_ids": ("source-1",),
                    "claim": "orders are relevant",
                    "candidate_targets": (
                        {"target_kind": "table", "table": "public.orders"},
                    ),
                    "citation_evidence_ids": ("evidence-1",),
                },
            ),
            "next": {
                "next_kind": "tool",
                "hypothesis_ref": {
                    "reference_kind": "proposed",
                    "proposal_key": "proposal:h1",
                },
                "intent": {
                    "tool_name": "inspect_table",
                    "arguments": {"table": "public.orders"},
                },
            },
        },
        strict=True,
    )

    resolved = resolve_research_decision(
        state,
        decision,
        loaded_schema=loaded,
        freshness_context=_freshness(state),
        registry=_registry(namespace),
    )

    assert resolved.admission.action is not None
    assert resolved.admission.action.hypothesis_id is not None
    assert resolved.admission.action.hypothesis_id.startswith("hypothesis:")
    assert (
        resolved.admission.hypotheses[0].hypothesis_id
        == resolved.admission.action.hypothesis_id
    )


def test_derived_expression_remains_a_claim_and_is_never_put_in_tool_arguments() -> (
    None
):
    loaded, namespace = _schema()
    state = _state(namespace, with_evidence=True, required=False)
    expression_claim = "status derived from an external business rule"
    decision = ResearchDecisionV1.model_validate(
        {
            "proposals": (
                {
                    "proposal_type": "new_binding",
                    "proposal_key": "proposal:b1",
                    "source_id": "source-1",
                    "candidate": {
                        "kind": "derived_expression",
                        "expression_claim": expression_claim,
                        "document_id": "schema-doc",
                        "rule_excerpt": "Orders schema",
                        "input_columns": (
                            {"table": "public.orders", "column": "status"},
                            {"table": "public.orders", "column": "id"},
                        ),
                    },
                    "join_references": (),
                    "citation_evidence_ids": ("evidence-1",),
                },
            ),
            "next": {
                "next_kind": "tool",
                "hypothesis_ref": None,
                "intent": {
                    "tool_name": "inspect_column",
                    "arguments": {"table": "public.orders", "column": "status"},
                },
            },
        },
        strict=True,
    )

    resolved = resolve_research_decision(
        state,
        decision,
        loaded_schema=loaded,
        freshness_context=_freshness(state),
        registry=_registry(namespace),
    )

    binding = resolved.admission.bindings[0]
    assert binding.status is BindingStatus.CANDIDATE
    assert binding.expression.expression == expression_claim
    assert binding.document.document_id == "schema-doc"
    assert binding.rule_excerpt == "Orders schema"
    assert [
        column.column for column in binding.input_columns
    ] == ["status", "id"]
    assert resolved.invocation is not None
    assert expression_claim not in str(resolved.invocation.tool_call.arguments)
    assert [item.logical_column for item in resolved.semantic_batch.columns] == [
        "id",
        "status",
    ]


def test_resolution_is_deterministic_after_decision_proposal_reordering() -> None:
    loaded, namespace = _schema()
    state = _state(namespace, with_evidence=True, required=False)
    proposals = (
        {
            "proposal_type": "new_hypothesis",
            "proposal_key": "proposal:h2",
            "source_ids": ("source-1",),
            "claim": "customers are relevant",
            "candidate_targets": (
                {"target_kind": "table", "table": "public.customers"},
            ),
            "citation_evidence_ids": ("evidence-1",),
        },
        {
            "proposal_type": "new_hypothesis",
            "proposal_key": "proposal:h1",
            "source_ids": ("source-1",),
            "claim": "orders are relevant",
            "candidate_targets": ({"target_kind": "table", "table": "public.orders"},),
            "citation_evidence_ids": ("evidence-1",),
        },
    )

    def decision(values) -> ResearchDecisionV1:
        return ResearchDecisionV1.model_validate(
            {
                "proposals": values,
                "next": {
                    "next_kind": "tool",
                    "hypothesis_ref": None,
                    "intent": {
                        "tool_name": "inspect_column",
                        "arguments": {"table": "public.orders", "column": "status"},
                    },
                },
            },
            strict=True,
        )

    first = resolve_research_decision(
        state,
        decision(proposals),
        loaded_schema=loaded,
        freshness_context=_freshness(state),
        registry=_registry(namespace),
    )
    second = resolve_research_decision(
        state,
        decision(tuple(reversed(proposals))),
        loaded_schema=loaded,
        freshness_context=_freshness(state),
        registry=_registry(namespace),
    )

    assert first.decision_digest == second.decision_digest
    assert first.tool_claim == second.tool_claim
    assert first.invocation is not None and second.invocation is not None
    assert first.invocation.tool_call == second.invocation.tool_call
    assert first.invocation.invocation_id == second.invocation.invocation_id


def test_module_has_no_model_database_persistence_or_state_mutation_boundary() -> None:
    import custom_tools.text_to_sql.adaptive.decision_resolver as module

    source = module.__file__
    assert source is not None
    text = Path(source).read_text(encoding="utf-8")
    assert "ToolCallingAgent" not in text
    assert "load_scoped_schema(" not in text
    assert "commit_semantic_turn" not in text
    assert "adaptive_state_store" not in text
    assert "state_store" not in text
